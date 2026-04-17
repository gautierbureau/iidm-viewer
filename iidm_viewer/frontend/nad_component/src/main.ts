/**
 * Streamlit custom component — interactive NAD viewer.
 *
 * Thin wrapper around @powsybl/network-viewer-core. All rendering,
 * hit-testing, pan/zoom and drag come from the library. Our only
 * responsibilities are:
 *   1. speak the Streamlit iframe wire protocol
 *      (componentReady / render / setComponentValue / setFrameHeight);
 *   2. translate onSelectNodeCallback into a setComponentValue payload
 *      that matches the Stage-1 Python contract:
 *        {type: "nad-vl-click", vl: <equipmentId>, ts: <Date.now()>}
 *
 * The Python contract (render_interactive_nad(svg, metadata, height, key))
 * is unchanged from Stage 1.
 */
import {
  NetworkAreaDiagramViewer,
  type DiagramMetadata,
} from '@powsybl/network-viewer-core';

type RenderArgs = {
  svg?: string;
  metadata?: string;
  height?: number;
};

const ROOT_ID = 'nad';

let viewer: NetworkAreaDiagramViewer | null = null;

function sendParent(msg: unknown): void {
  window.parent.postMessage(msg, '*');
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

function parseMetadata(raw: string | undefined): DiagramMetadata | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as DiagramMetadata;
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

  viewer = new NetworkAreaDiagramViewer(
    root,
    svgContent,
    metadata,
    {
      enableDragInteraction: true,
      onSelectNodeCallback: (equipmentId: string) => {
        setComponentValue({
          type: 'nad-vl-click',
          vl: equipmentId,
          ts: Date.now(),
        });
      },
    }
  );

  setFrameHeight(height);
}

window.addEventListener('message', (e: MessageEvent) => {
  const data = e.data as { type?: string; args?: RenderArgs } | null;
  if (!data || data.type !== 'streamlit:render') return;
  render(data.args ?? {});
});

sendParent({ type: 'streamlit:componentReady', apiVersion: 1 });

// Silence "assigned but never used" from strict mode when we only
// instantiate the viewer for side effects (the library keeps itself
// alive via DOM listeners).
void viewer;
