/**
 * Global CSRF token wiring.
 *
 * The server sets a JS-readable `csrf_token` cookie and enforces a
 * double-submit check in CSRFMiddleware: every state-changing request
 * (POST/PUT/PATCH/DELETE) on a non-API route must echo that token back via
 * the `X-CSRF-Token` header or a `csrf_token` form field, and the
 * Origin/Referer must match the host.
 *
 * This file attaches the token automatically to the three request paths
 * used in this app so legitimate flows keep working:
 *   1. htmx requests (hx-post / hx-get with side effects)
 *   2. raw fetch() calls (delete/danger-zone actions, domain settings)
 *   3. classic <form method="post"> submits (wizard, admin user forms)
 *
 * Loaded globally via base.html.j2 (additionalJs).
 */
(function () {
  "use strict";

  var COOKIE_NAME = "csrf_token";
  var HEADER_NAME = "X-CSRF-Token";
  var FIELD_NAME = "csrf_token";

  function getToken() {
    var prefix = COOKIE_NAME + "=";
    var parts = document.cookie ? document.cookie.split("; ") : [];
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].indexOf(prefix) === 0) {
        return decodeURIComponent(parts[i].substring(prefix.length));
      }
    }
    return null;
  }

  function isSameOrigin(url) {
    try {
      var u = new URL(url, window.location.href);
      return u.origin === window.location.origin;
    } catch (e) {
      // Relative URL that failed to parse is same-origin by definition.
      return true;
    }
  }

  // 1. htmx
  document.body.addEventListener("htmx:configRequest", function (evt) {
    var token = getToken();
    if (token) {
      evt.detail.headers[HEADER_NAME] = token;
    }
  });

  // 2. fetch
  var nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function (input, init) {
      init = init || {};
      var method = (init.method || (typeof input !== "string" && input && input.method) || "GET").toUpperCase();
      var url = typeof input === "string" ? input : input && input.url;
      if (method !== "GET" && method !== "HEAD" && (url === undefined || isSameOrigin(url))) {
        var token = getToken();
        if (token) {
          var headers = new Headers(init.headers || (typeof input !== "string" && input && input.headers) || {});
          if (!headers.has(HEADER_NAME)) {
            headers.set(HEADER_NAME, token);
          }
          init.headers = headers;
        }
      }
      return nativeFetch(input, init);
    };
  }

  // 3. classic form submits
  document.addEventListener(
    "submit",
    function (evt) {
      var form = evt.target;
      if (!form || form.tagName !== "FORM") {
        return;
      }
      var method = (form.getAttribute("method") || "GET").toUpperCase();
      if (method === "GET") {
        return;
      }
      if (form.querySelector('input[name="' + FIELD_NAME + '"]')) {
        return;
      }
      var token = getToken();
      if (!token) {
        return;
      }
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = FIELD_NAME;
      hidden.value = token;
      form.appendChild(hidden);
    },
    true
  );
})();
