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
 */
import {
  SingleLineDiagramViewer,
  type SLDMetadata,
} from '@powsybl/network-viewer-core';

type RenderArgs = {
  svg?: string;
  metadata?: string;
  height?: number;
  svgType?: string;
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
