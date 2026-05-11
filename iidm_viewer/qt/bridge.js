// Streamlit-iframe-protocol shim for the PySide6 host.
//
// The existing JS bundles in ``frontend/*_component/dist/`` speak the
// Streamlit wire protocol (``window.parent.postMessage({isStreamlitMessage,
// type: 'streamlit:...'})``). Inside QWebEngineView there is no parent
// iframe — ``window.parent === window`` — so those postMessages land
// back in the bundle's own window. This shim adapts them to a
// QWebChannel-exposed Python object named ``iidm_bridge``:
//
//   bundle -> setComponentValue(value)  ->  iidm_bridge.onComponentValue(json)
//   Python -> window.iidmRender(args)   ->  bundle's 'streamlit:render' listener
//
// Injected at DocumentCreation so ``window.iidmRender`` exists before
// the bundle's module script runs. The qwebchannel.js source is
// prepended at injection time (see ``web_view.py``).

(function () {
  'use strict';

  let pyBridge = null;
  const pendingValues = [];

  function flushPending() {
    if (!pyBridge) return;
    while (pendingValues.length) {
      const v = pendingValues.shift();
      try {
        pyBridge.onComponentValue(JSON.stringify(v));
      } catch (err) {
        console.error('[iidm-bridge] forward failed:', err);
      }
    }
  }

  // Python -> JS: ask the bundle to render with new args.
  window.iidmRender = function (args) {
    window.postMessage({ type: 'streamlit:render', args: args || {} }, '*');
  };

  // JS -> Python: catch every Streamlit-protocol postMessage and forward
  // setComponentValue payloads. componentReady / setFrameHeight are
  // swallowed silently (the iframe-height protocol is meaningless here:
  // the QWebEngineView fills its host widget).
  window.addEventListener('message', function (e) {
    const data = e.data;
    if (!data || data.isStreamlitMessage !== true) return;
    if (data.type === 'streamlit:setComponentValue') {
      if (pyBridge) {
        try {
          pyBridge.onComponentValue(JSON.stringify(data.value));
        } catch (err) {
          console.error('[iidm-bridge] forward failed:', err);
        }
      } else {
        pendingValues.push(data.value);
      }
    }
  });

  // QWebChannel handshake. qwebchannel.js is concatenated ahead of this
  // file at injection time.
  function connect() {
    if (typeof QWebChannel === 'undefined' || typeof qt === 'undefined' || !qt.webChannelTransport) {
      // Transport not ready yet — try again on the next tick.
      setTimeout(connect, 0);
      return;
    }
    new QWebChannel(qt.webChannelTransport, function (channel) {
      pyBridge = channel.objects.iidm_bridge;
      if (pyBridge && typeof pyBridge.onReady === 'function') {
        try {
          pyBridge.onReady();
        } catch (err) {
          console.error('[iidm-bridge] onReady failed:', err);
        }
      }
      flushPending();
    });
  }
  connect();
})();
