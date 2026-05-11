/**
 * Streamlit custom component — interactive geographical network map.
 *
 * Wraps @powsybl/network-map-layers (deck.gl layers designed for
 * Powsybl networks) on top of MapLibre GL JS with OSM raster tiles.
 * The library supplies:
 *   - SubstationLayer: concentric rings per substation, one ring per VL,
 *     coloured by nominal voltage;
 *   - LineLayer: lines/transformers rendered as polylines between the
 *     substations they connect, coloured by the higher of the two
 *     nominal voltages.
 *
 * Python -> JS args (via the Streamlit wire protocol):
 *   {
 *     substations: MapSubstation[],
 *     substationPositions: GeoDataSubstation[],
 *     lines: MapLine[],                   // lines + 2W-transformers, untyped
 *     linePositions: {id: string, coordinates: Coordinate[]}[],
 *     height: number,
 *   }
 *
 * JS -> Python (setComponentValue):
 *   { type: 'map-substation-click', substationId, vlIds: string[], ts }
 *     when the user clicks a substation. ``vlIds`` is ordered by
 *     descending nominal voltage so the host can default to the
 *     highest-V VL when navigating to the SLD.
 *
 * Tooltips remain handled entirely in the browser (parity with the
 * previous Leaflet implementation).
 *
 * Uses MapboxOverlay (interleaved: false) from @deck.gl/mapbox on top
 * of MapLibre GL JS v4, matching the integration used by
 * @powsybl/network-viewer.
 */
import { MapboxOverlay } from '@deck.gl/mapbox';
import maplibregl, { LngLatBoundsLike, StyleSpecification } from 'maplibre-gl';
import {
  EQUIPMENT_TYPES,
  GeoData,
  getNominalVoltageColor,
  LineLayer,
  MapEquipments,
  SubstationLayer,
  type Coordinate,
  type GeoDataSubstation,
  type MapLine,
  type MapLineWithType,
  type MapSubstation,
} from '@powsybl/network-map-layers';

type LinePosition = { id: string; coordinates: Coordinate[] };

type FlyToRequest = {
  substationId?: string;
  lon?: number;
  lat?: number;
  zoom?: number;
  ts?: number;
};

type RenderArgs = {
  substations?: MapSubstation[];
  substationPositions?: GeoDataSubstation[];
  lines?: MapLine[];
  linePositions?: LinePosition[];
  version?: number;
  height?: number;
  // Optional "fly the camera to this substation / coordinate" hint.
  // Hosts use it to implement cross-tab navigation (e.g. SLD-feeder
  // click -> Map focuses the substation at the line's other end).
  // The ``ts`` field is honoured for change-detection: every new
  // request must carry a fresh ``ts`` (Date.now()) to fire again.
  flyTo?: FlyToRequest;
};

const ROOT_ID = 'map';
// OSM raster style: no API key, same tile source as the previous
// Leaflet implementation. MapLibre renders the tiles via WebGL so we
// can stack deck.gl layers on top through MapboxOverlay.
const OSM_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: 'raster',
      tiles: [
        'https://a.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://b.tile.openstreetmap.org/{z}/{x}/{y}.png',
        'https://c.tile.openstreetmap.org/{z}/{x}/{y}.png',
      ],
      tileSize: 256,
      attribution: '&copy; OpenStreetMap contributors',
    },
  },
  layers: [{ id: 'osm-tiles', type: 'raster', source: 'osm', minzoom: 0, maxzoom: 19 }],
};

const DEFAULT_CENTER: [number, number] = [2.5, 46.6]; // France fallback
const DEFAULT_ZOOM = 5;

let map: maplibregl.Map | null = null;
let overlay: MapboxOverlay | null = null;
let legendEl: HTMLDivElement | null = null;
let tooltipEl: HTMLDivElement | null = null;
let lastDataVersion = -1;
// Cache of substation coordinates so subsequent ``flyTo({substationId})``
// hits work without the host having to re-send the whole geometry.
const substationCoords: Map<string, Coordinate> = new Map();
// Last applied flyTo timestamp — every new request must carry a
// fresh ``ts`` (Date.now()) to fire again.
let lastFlyToTs = -1;
const DEFAULT_FLY_ZOOM = 11;

function sendParent(msg: Record<string, unknown>): void {
  // Streamlit drops any postMessage whose payload lacks the
  // `isStreamlitMessage` marker (checked via Object.hasOwn), so the
  // iframe handshake never completes without it.
  window.parent.postMessage({ isStreamlitMessage: true, ...msg }, '*');
}

function setFrameHeight(h: number): void {
  sendParent({ type: 'streamlit:setFrameHeight', height: h });
}

function computeBounds(positions: GeoDataSubstation[]): LngLatBoundsLike | null {
  if (positions.length === 0) return null;
  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  for (const p of positions) {
    const { lon, lat } = p.coordinate;
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }
  // Tiny bbox -> pad so fitBounds doesn't max out zoom.
  const pad = 0.05;
  return [
    [minLon - pad, minLat - pad],
    [maxLon + pad, maxLat + pad],
  ];
}

function buildLegend(nominalVoltages: number[]): HTMLDivElement {
  const div = document.createElement('div');
  div.className = 'map-legend';
  let html = '<b>Nominal voltage</b>';
  for (const nv of nominalVoltages) {
    const [r, g, b] = getNominalVoltageColor(nv);
    html +=
      `<div><span class="swatch" style="background:rgb(${r},${g},${b})"></span>` +
      `${nv.toFixed(0)} kV</div>`;
  }
  div.innerHTML = html;
  return div;
}

function ensureTooltipEl(root: HTMLElement): HTMLDivElement {
  if (tooltipEl && tooltipEl.parentElement === root) return tooltipEl;
  tooltipEl = document.createElement('div');
  tooltipEl.className = 'map-tooltip';
  tooltipEl.style.display = 'none';
  root.appendChild(tooltipEl);
  return tooltipEl;
}

function formatSubstationTooltip(sub: MapSubstation): string {
  const rows = sub.voltageLevels
    .map((vl) => {
      const [r, g, b] = getNominalVoltageColor(vl.nominalV);
      const sw = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:rgb(${r},${g},${b});margin-right:4px;"></span>`;
      const nm = vl.substationName || vl.id;
      return `${sw}${vl.id}${nm && nm !== vl.id ? ` (${nm})` : ''} ${vl.nominalV} kV`;
    })
    .join('<br>');
  const header = sub.name && sub.name !== sub.id ? `${sub.id} (${sub.name})` : sub.id;
  return `<b>${header}</b><br>${rows}`;
}

function formatLineTooltip(line: MapLine): string {
  const header = line.name && line.name !== line.id ? `${line.id} (${line.name})` : line.id;
  const p1 = Math.abs(line.p1 ?? 0).toFixed(1);
  const i1 = (line.i1 ?? 0).toFixed(1);
  return `<b>${header}</b><br>P1: ${p1} MW, I1: ${i1} A`;
}

function emitSubstationClick(sub: MapSubstation): void {
  // Order voltage levels by descending nominal voltage so hosts that
  // default to "first" land on the most meaningful VL (e.g. 400 kV
  // before 90 kV).
  const vlIds = [...sub.voltageLevels]
    .sort((a, b) => (b.nominalV ?? 0) - (a.nominalV ?? 0))
    .map((v) => v.id);
  setComponentValue({
    type: 'map-substation-click',
    substationId: sub.id,
    vlIds,
    ts: Date.now(),
  });
}

function buildLayers(
  typedLines: MapLineWithType[],
  network: MapEquipments,
  geoData: GeoData,
  substations: MapSubstation[],
) {
  return [
    new LineLayer({
      id: 'powsybl-lines',
      data: typedLines,
      network,
      geoData,
      getNominalVoltageColor,
      disconnectedLineColor: [100, 100, 100, 255],
      filteredNominalVoltages: network.getNominalVoltages(),
      labelsVisible: false,
      labelSize: 11,
      labelColor: [0, 0, 0, 255],
      lineFullPath: true,
      lineParallelPath: true,
      showLineFlow: false,
      areFlowsValid: false,
      updatedLines: [],
      pickable: true,
    }),
    new SubstationLayer({
      id: 'powsybl-substations',
      data: substations,
      network,
      geoData,
      getNominalVoltageColor,
      filteredNominalVoltages: null,
      labelsVisible: false,
      labelColor: [0, 0, 0, 255],
      labelSize: 12,
      getNameOrId: (s: MapSubstation) => s.name || s.id,
      pickable: true,
      onClick: (info: { object?: unknown }) => {
        const obj = info?.object;
        if (obj && typeof obj === 'object' && 'voltageLevels' in obj) {
          emitSubstationClick(obj as MapSubstation);
        }
      },
    }),
  ];
}

function applyFlyTo(req: FlyToRequest | undefined): void {
  if (!map || !req) return;
  if (typeof req.ts === 'number' && req.ts === lastFlyToTs) return;
  let lon: number | undefined = req.lon;
  let lat: number | undefined = req.lat;
  if ((lon === undefined || lat === undefined) && req.substationId) {
    const cached = substationCoords.get(req.substationId);
    if (cached) {
      lon = cached.lon;
      lat = cached.lat;
    }
  }
  if (lon === undefined || lat === undefined) return;
  const zoom = typeof req.zoom === 'number' ? req.zoom : DEFAULT_FLY_ZOOM;
  // ``easeTo`` rather than ``flyTo`` keeps the animation short for
  // map-tab return trips; ``flyTo`` is dramatic but slow for the
  // back-and-forth cadence the cross-tab navigation produces.
  map.easeTo({ center: [lon, lat], zoom, duration: 600 });
  if (typeof req.ts === 'number') lastFlyToTs = req.ts;
}

function render(args: RenderArgs): void {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;

  const height = typeof args.height === 'number' ? args.height : 670;
  root.style.width = '100%';
  root.style.height = `${height}px`;

  const substations = args.substations ?? [];
  const substationPositions = args.substationPositions ?? [];
  const lines = args.lines ?? [];
  const linePositions = args.linePositions ?? [];

  // ------------------------------------------------------------------
  // Build MapEquipments / GeoData.
  // MapEquipments wants lines untyped; the LineLayer wants each line
  // augmented with `equipmentType`. Build both views here.
  // ------------------------------------------------------------------
  const network = new MapEquipments();
  network.updateSubstations(substations, true);
  network.updateLines(lines, true);

  const subPosMap = new Map<string, Coordinate>();
  for (const p of substationPositions) {
    subPosMap.set(p.id, p.coordinate);
    // Mirror into the module-level cache so subsequent ``flyTo``
    // hits work even when the host sends a render without positions
    // (the wrapper already does this when data is unchanged).
    substationCoords.set(p.id, p.coordinate);
  }

  const linePosMap = new Map<string, Coordinate[]>();
  for (const lp of linePositions) linePosMap.set(lp.id, lp.coordinates);

  const geoData = new GeoData(subPosMap, linePosMap);

  const typedLines: MapLineWithType[] = lines.map((l) => ({
    ...l,
    equipmentType: EQUIPMENT_TYPES.LINE,
  }));

  // ------------------------------------------------------------------
  // MapLibre base map.
  // If the map already exists (same component instance across reruns),
  // skip teardown and just push new layers to the existing overlay.
  // This avoids a full WebGL context rebuild + OSM tile reload on every
  // Streamlit rerun triggered by VL navigation.
  // ------------------------------------------------------------------
  const dataVersion = typeof args.version === 'number' ? args.version : 0;
  if (map && overlay) {
    if (dataVersion !== lastDataVersion) {
      // Network data changed (new load, topology edit) — rebuild layers.
      lastDataVersion = dataVersion;
      overlay.setProps({ layers: buildLayers(typedLines, network, geoData, substations) });
      if (legendEl && legendEl.parentElement) legendEl.parentElement.removeChild(legendEl);
      const present = network.getNominalVoltages();
      if (present.length > 0) {
        legendEl = buildLegend(present);
        root.appendChild(legendEl);
      }
    }
    // Whether or not we rebuilt, height must be reported every render.
    setFrameHeight(height);
    // Honour the optional flyTo hint on every render so a no-op data
    // refresh that *only* carries a flyTo still animates.
    applyFlyTo(args.flyTo);
    return;
  }

  // First render: create the map from scratch.
  lastDataVersion = dataVersion;
  root.innerHTML = '';

  map = new maplibregl.Map({
    container: root,
    style: OSM_STYLE,
    center: DEFAULT_CENTER,
    zoom: DEFAULT_ZOOM,
  });

  const bounds = computeBounds(substationPositions);
  const initialFlyTo = args.flyTo;
  map.on('load', () => {
    if (!map) return;
    if (bounds) map.fitBounds(bounds, { padding: 40, duration: 0 });

    overlay = new MapboxOverlay({
      interleaved: false,
      layers: buildLayers(typedLines, network, geoData, substations),
    });

    map.addControl(overlay as unknown as maplibregl.IControl);

    // If the very first render carried a flyTo (e.g. the host
    // restored state from a previous session), apply it after the
    // initial fitBounds so the user lands at the requested spot.
    applyFlyTo(initialFlyTo);

    // Diagnostics: check that the deck.gl overlay is properly set up.
    setTimeout(() => {
      const deck = (overlay as any)?._deck;
      console.info('[map-diag] overlay._deck exists:', !!deck);
      console.info('[map-diag] deck.isInitialized:', deck?.isInitialized);
      const deckCanvas = root.querySelector('canvas:not(.maplibregl-canvas)');
      console.info('[map-diag] deck canvas:', deckCanvas
        ? `${(deckCanvas as HTMLCanvasElement).width}x${(deckCanvas as HTMLCanvasElement).height}`
        : 'MISSING');
      console.info('[map-diag] deck viewState:', JSON.stringify(deck?.viewManager?.getViewState?.()));
      // List sub-layers to check if LineLayer actually produced sub-layers.
      const layerIds = deck?.props?.layers?.map((l: any) => l.id) ?? [];
      console.info('[map-diag] top-level layer ids:', layerIds);
      const internalLayers = deck?.layerManager?.getLayers?.()?.map((l: any) => l.id) ?? [];
      console.info('[map-diag] all rendered layer ids:', internalLayers);
    }, 1000);

    // Log the first few 'render' events to confirm viewport sync fires.
    let renderCount = 0;
    map.on('render', () => {
      renderCount++;
      if (renderCount <= 5) {
        const { lng, lat } = map!.getCenter();
        console.info(`[map-diag] render #${renderCount} center=(${lng.toFixed(3)},${lat.toFixed(3)}) zoom=${map!.getZoom().toFixed(2)}`);
      }
    });

    // Log the first few 'move' events.
    let moveCount = 0;
    map.on('move', () => {
      moveCount++;
      if (moveCount <= 10) {
        const { lng, lat } = map!.getCenter();
        console.info(`[map-diag] move #${moveCount} center=(${lng.toFixed(3)},${lat.toFixed(3)}) zoom=${map!.getZoom().toFixed(2)}`);
      }
    });
  });

  // ------------------------------------------------------------------
  // Tooltip — handled in the browser (parity with previous Leaflet).
  // ------------------------------------------------------------------
  const tooltip = ensureTooltipEl(root);
  map.on('mousemove', (e) => {
    if (!overlay) return;
    const pick = overlay.pickObject({ x: e.point.x, y: e.point.y, radius: 4 });
    if (!pick || !pick.object) {
      tooltip.style.display = 'none';
      return;
    }
    const obj = pick.object as MapSubstation | MapLine | MapLineWithType;
    let html: string;
    if ('voltageLevels' in obj) {
      html = formatSubstationTooltip(obj as MapSubstation);
    } else {
      html = formatLineTooltip(obj as MapLine);
    }
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';
    tooltip.style.left = `${e.point.x + 10}px`;
    tooltip.style.top = `${e.point.y + 10}px`;
  });
  map.on('mouseout', () => {
    tooltip.style.display = 'none';
  });

  // ------------------------------------------------------------------
  // Legend — DOM overlay (cheaper than a deck.gl layer).
  // ------------------------------------------------------------------
  if (legendEl && legendEl.parentElement) legendEl.parentElement.removeChild(legendEl);
  const present = network.getNominalVoltages();
  if (present.length > 0) {
    legendEl = buildLegend(present);
    root.appendChild(legendEl);
  }

  setFrameHeight(height);
}

window.addEventListener('message', (e: MessageEvent) => {
  const data = e.data as { type?: string; args?: RenderArgs } | null;
  if (!data || data.type !== 'streamlit:render') return;
  render(data.args ?? {});
});

sendParent({ type: 'streamlit:componentReady', apiVersion: 1 });
