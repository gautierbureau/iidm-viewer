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
 *     height: number,
 *   }
 *
 * JS -> Python: nothing for now (tooltips handled entirely in the
 * browser, matching parity with the previous Leaflet implementation).
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

type RenderArgs = {
  substations?: MapSubstation[];
  substationPositions?: GeoDataSubstation[];
  lines?: MapLine[];
  height?: number;
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

function render(args: RenderArgs): void {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;

  const height = typeof args.height === 'number' ? args.height : 670;
  root.style.width = '100%';
  root.style.height = `${height}px`;

  const substations = args.substations ?? [];
  const substationPositions = args.substationPositions ?? [];
  const lines = args.lines ?? [];

  // ------------------------------------------------------------------
  // Build MapEquipments / GeoData.
  // MapEquipments wants lines untyped; the LineLayer wants each line
  // augmented with `equipmentType`. Build both views here.
  // ------------------------------------------------------------------
  const network = new MapEquipments();
  network.updateSubstations(substations, true);
  network.updateLines(lines, true);

  const subPosMap = new Map<string, Coordinate>();
  for (const p of substationPositions) subPosMap.set(p.id, p.coordinate);
  const geoData = new GeoData(subPosMap, new Map());

  const typedLines: MapLineWithType[] = lines.map((l) => ({
    ...l,
    equipmentType: EQUIPMENT_TYPES.LINE,
  }));

  // ------------------------------------------------------------------
  // MapLibre base map.
  // ------------------------------------------------------------------
  if (map) {
    map.remove();
    map = null;
    overlay = null;
  }
  root.innerHTML = '';

  map = new maplibregl.Map({
    container: root,
    style: OSM_STYLE,
    center: DEFAULT_CENTER,
    zoom: DEFAULT_ZOOM,
  });

  const bounds = computeBounds(substationPositions);
  map.on('load', () => {
    if (!map) return;
    if (bounds) map.fitBounds(bounds, { padding: 40, duration: 0 });

    overlay = new MapboxOverlay({
      interleaved: false,
      layers: [
        new LineLayer({
          id: 'powsybl-lines',
          data: typedLines,
          network,
          geoData,
          getNominalVoltageColor,
          disconnectedLineColor: [204, 204, 204, 255],
          filteredNominalVoltages: network.getNominalVoltages(),
          labelsVisible: false,
          labelSize: 11,
          labelColor: [0, 0, 0, 255],
          lineFullPath: false,
          lineParallelPath: true,
          showLineFlow: true,
          areFlowsValid: true,
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
        }),
      ],
    });

    // MapboxOverlay is a `maplibregl.IControl`-compatible control.
    map.addControl(overlay as unknown as maplibregl.IControl);
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
