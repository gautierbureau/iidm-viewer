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
      // enableAdaptiveTextZoom makes the library call createLegendBox() during
      // init, which creates interactive <div class="nad-label-box"> elements
      // in its own <foreignObject class="nad-text-nodes"> container.
      // Setting the threshold to MAX_VALUE ensures labels are always rendered
      // (the adaptive path only hides labels when maxDisplayedSize > threshold).
      enableAdaptiveTextZoom: true,
      adaptiveTextZoomThreshold: Number.MAX_VALUE,
      onSelectNodeCallback: (equipmentId: string) => {
        setComponentValue({
          type: 'nad-vl-click',
          vl: equipmentId,
          ts: Date.now(),
        });
      },
    }
  );

  // pypowsybl's Java library emits each text node as an individual
  // <foreignObject id="textNodeId" x="…" y="…"> inside a
  // <g class="nad-text-nodes"> group.  The JS viewer library creates its own
  // <foreignObject class="nad-text-nodes"> container with interactive
  // nad-label-box divs (via createLegendBox, triggered above by
  // enableAdaptiveTextZoom).  We must remove the Java-generated SVG groups so
  // that querySelector("[id='<legendSvgId>']") resolves to the interactive divs
  // rather than the original foreignObjects, whose SVG x/y attributes cannot
  // be updated via the CSS left/top writes that updateTextNodePosition uses.
  root.querySelectorAll('g.nad-text-nodes').forEach((el) => el.remove());

  // Ensure the foreignObject that hosts interactive label boxes is
  // styled correctly.  Some pypowsybl versions omit the CSS rule
  //   foreignObject.nad-text-nodes { overflow: visible; color: black }
  // from the SVG <style> block, which leaves the foreignObject clipped
  // and text colorless.  Apply these properties explicitly so labels
  // render regardless of the pypowsybl version.
  // The library may create the foreignObject asynchronously (debounced
  // MutationObserver on viewBox changes), so defer the patch.
  const patchForeignObject = () => {
    const fo = root.querySelector('foreignObject.nad-text-nodes');
    if (fo) {
      (fo as SVGForeignObjectElement).style.overflow = 'visible';
      (fo as SVGForeignObjectElement).style.color = 'black';
    }
  };
  patchForeignObject();
  // Retry once after the library's debounced init (50ms) completes.
  setTimeout(patchForeignObject, 100);

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
