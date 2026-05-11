/**
 * Streamlit custom component — interactive SLD viewer.
 *
 * Thin wrapper around @powsybl/network-viewer-core's
 * SingleLineDiagramViewer. The library draws the SLG, places the
 * "next voltage level" navigation arrows (one per feeder pointing to an
 * adjacent VL), and hit-tests them. Our only responsibilities are:
 *   1. speak the Streamlit iframe wire protocol
 *      (componentReady / render / setComponentValue / setFrameHeight);
 *   2. translate onNextVoltageCallback(nextVId) into a setComponentValue
 *      payload:  {type: "sld-vl-click", vl: <nextVId>, ts: <Date.now()>}
 *
 * Python contract (stable):
 *   render_interactive_sld(svg, metadata, height, key) ->
 *       None
 *       | {"type": "sld-vl-click",     "vl": "VLx",   "ts": <ms>}
 *       | {"type": "sld-breaker-click","breakerId": "SW1", "open": true, "ts": <ms>}
 *
 * "open" in sld-breaker-click is the *desired new state* (already toggled by
 * the library before the callback fires).
 *
 * Optional render arg ``preserveViewport`` (default false): when true,
 * the pan/zoom state of the previous render is captured via
 * ``viewer.getViewBox()`` and restored on the new viewer via
 * ``viewer.setViewBox(...)``. The PySide6 host opts in so VL→VL
 * navigation and same-VL re-renders (after a switch toggle, a data
 * edit, …) feel continuous instead of snapping back to the SVG's
 * auto-fit. Streamlit and NiceGUI leave it off — their default
 * fit-on-render behaviour is unchanged.
 *
 * Note on ``setSvgContent``: the underlying library exposes it but
 * the implementation is a one-line property setter (it does not
 * re-render). Preserving pan/zoom across renders therefore goes
 * through the viewBox round-trip; we still rebuild the viewer
 * instance on every render.
 */
import {
  SingleLineDiagramViewer,
  type SLDMetadata,
} from '@powsybl/network-viewer-core';

type ViewBoxLike = { x: number; y: number; width: number; height: number };

type RenderArgs = {
  svg?: string;
  metadata?: string;
  height?: number;
  svgType?: string;
  preserveViewport?: boolean;
};

const ROOT_ID = 'sld';

let viewer: SingleLineDiagramViewer | null = null;

function sendParent(msg: Record<string, unknown>): void {
  // Streamlit drops any postMessage whose payload lacks the
  // `isStreamlitMessage` marker (checked via Object.hasOwn), so the
  // iframe handshake never completes without it.
  window.parent.postMessage({ isStreamlitMessage: true, ...msg }, '*');
}

function setComponentValue(value: unknown): void {
  sendParent({
    type: 'streamlit:setComponentValue',
    dataType: 'json',
    value,
  });
}

function setFrameHeight(h: number): void {
  sendParent({ type: 'streamlit:setFrameHeight', height: h });
}

function parseMetadata(raw: string | undefined): SLDMetadata | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as SLDMetadata;
  } catch {
    return null;
  }
}

function render(args: RenderArgs): void {
  const root = document.getElementById(ROOT_ID);
  if (!root) return;

  // Pan/zoom continuity (opt-in via ``preserveViewport``). Captured
  // before the tear-down on the next line and applied on the new
  // viewer after construction. ``getViewBox`` returns ``undefined``
  // when the previous viewer never finished its init, hence the
  // try/catch + null-check.
  let savedViewBox: ViewBoxLike | null = null;
  if (args.preserveViewport && viewer) {
    try {
      const vb = viewer.getViewBox();
      if (vb && typeof vb.width === 'number' && typeof vb.height === 'number') {
        savedViewBox = { x: vb.x, y: vb.y, width: vb.width, height: vb.height };
      }
    } catch {
      // Library can throw if svgDraw is gone; fall through to a clean fit.
    }
  }

  root.innerHTML = '';
  const height = typeof args.height === 'number' ? args.height : 700;
  root.style.width = '100%';
  root.style.height = `${height}px`;

  const svgContent = args.svg ?? '';
  const metadata = parseMetadata(args.metadata);
  // "voltage-level" applies VL-scoped zoom limits; "substation" enables
  // the multi-VL substation layout. Fall back to "voltage-level" for any
  // unknown value so the viewer always initialises safely.
  const svgType = args.svgType === 'substation' ? 'substation' : 'voltage-level';

  // Constructor signature (see index.d.ts:490):
  //   container, svgContent, svgMetadata, svgType,
  //   minWidth, minHeight, maxWidth, maxHeight,
  //   onNextVoltageCallback, onBreakerCallback,
  //   onFeederCallback, onBusCallback,
  //   selectionBackColor, onToggleHoverCallback
  viewer = new SingleLineDiagramViewer(
    root,
    svgContent,
    metadata,
    svgType,
    0,
    0,
    10000,
    10000,
    (nextVId: string) => {
      setComponentValue({
        type: 'sld-vl-click',
        vl: nextVId,
        ts: Date.now(),
      });
    },
    (breakerId: string, open: boolean) => {
      setComponentValue({
        type: 'sld-breaker-click',
        breakerId,
        open,
        ts: Date.now(),
      });
    },
    null,
    null,
    '#009eff',
    null
  );

  if (savedViewBox && viewer) {
    try {
      viewer.setViewBox(savedViewBox);
      // The panZoom plugin clamps to min/max zoom on its next render;
      // refreshZoom() runs that clamp now so we don't ship an
      // out-of-range zoom for one frame.
      viewer.refreshZoom();
    } catch {
      // Best-effort restore: a viewBox from a wildly different VL
      // may not survive validation; the library's auto-fit takes
      // over silently.
    }
  }

  setFrameHeight(height);
}

window.addEventListener('message', (e: MessageEvent) => {
  const data = e.data as { type?: string; args?: RenderArgs } | null;
  if (!data || data.type !== 'streamlit:render') return;
  render(data.args ?? {});
});

sendParent({ type: 'streamlit:componentReady', apiVersion: 1 });

// Library keeps itself alive via DOM listeners; reference to silence
// strict-mode "assigned but never used".
void viewer;
