(() => {
  const module = document.currentScript.dataset.module;
  const prefix = '/' + module;
  function scoped(input) {
    const url = new URL(input, location.href);
    if (url.origin === location.origin && /^\/(api|assets|_hoard)\//.test(url.pathname)) url.pathname = prefix + url.pathname;
    return url.href;
  }
  const fetchNative = window.fetch.bind(window);
  const RequestNative = window.Request;
  window.fetch = (input, init) => fetchNative(input instanceof RequestNative
    ? new RequestNative(scoped(input.url), input) : scoped(String(input)), init);
  const EventSourceNative = window.EventSource;
  if (EventSourceNative) window.EventSource = class extends EventSourceNative {
    constructor(url, options) { super(scoped(String(url)), options); }
  };
  // Native clients are rebuilt with prefixed API/media literals as well. This
  // wrapper also covers the unchanged common copilot's runtime requests.
})();
