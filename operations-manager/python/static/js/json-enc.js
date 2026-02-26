/**
 * HTMX json-enc extension adapted for slash-path notation.
 *
 * Based on the TAD project's json-enc.js. Modified to treat `/` as a
 * segment separator in addition to `[` / `]` brackets. This allows form
 * field names like `services/keycloak/config/additional-clients[0]/redirect-uris[0]`
 * to produce nested JSON structures.
 */
htmx.defineExtension('json-enc', {
  onEvent: function (name, evt) {
    if (name === "htmx:configRequest") {
      evt.detail.headers['Content-Type'] = "application/json";
    }
  },

  encodeParameters: function (xhr, parameters, elt) {
    xhr.overrideMimeType('text/json');
    var options = this.getOptions(elt);
    return JSON.stringify(this.parseNestedParameters(parameters, options));
  },

  getOptions: function (elt) {
    var options = {
      cleanArrays: true
    };
    if (elt.getAttribute('data-json-enc-clean-arrays') === 'false') {
      options.cleanArrays = false;
    }
    return options;
  },

  parseNestedParameters: function (parameters, options) {
    var result = {};
    for (var key in parameters) {
      if (parameters.hasOwnProperty(key)) {
        this.parseKey(result, key, parameters[key]);
      }
    }
    if (options && options.cleanArrays) {
      this.cleanArrays(result);
    }
    return result;
  },

  parseKeyToSegments: function (key) {
    var segments = [];
    var currentSegment = '';
    var inBracket = false;

    for (var i = 0; i < key.length; i++) {
      var char = key[i];
      if (char === '[') {
        if (!inBracket) {
          // Empty brackets [] mean "array field" — strip them entirely
          // so that services[] becomes just ["services"] and the multi-value
          // array from HTMX is preserved as the direct value.
          if (i + 1 < key.length && key[i + 1] === ']') {
            i++; // skip the closing ]
          } else {
            if (currentSegment) {
              segments.push(currentSegment);
              currentSegment = '';
            }
            inBracket = true;
          }
        } else {
          currentSegment += char;
        }
      } else if (char === ']') {
        if (inBracket) {
          segments.push(currentSegment);
          currentSegment = '';
          inBracket = false;
        } else {
          currentSegment += char;
        }
      } else if (char === '/' && !inBracket) {
        if (currentSegment) {
          segments.push(currentSegment);
          currentSegment = '';
        }
      } else {
        currentSegment += char;
      }
    }
    if (currentSegment) {
      segments.push(currentSegment);
    }
    return segments;
  },

  parseKey: function (result, key, value) {
    var segments = this.parseKeyToSegments(key);
    if (segments.length === 0) {
      result[key] = value;
      return;
    }
    var current = result;
    for (var i = 0; i < segments.length - 1; i++) {
      var segment = segments[i];
      if (current[segment] === undefined) {
        current[segment] = {};
      }
      current = current[segment];
    }
    current[segments[segments.length - 1]] = value;
  },

  cleanArrays: function (obj) {
    if (!obj || typeof obj !== 'object') return;
    for (var key in obj) {
      var value = obj[key];
      if (value && typeof value === 'object') {
        this.cleanArrays(value);
        var keys = Object.keys(value);
        var isArray = keys.length > 0;
        for (var k = 0; k < keys.length; k++) {
          if (isNaN(parseInt(keys[k], 10))) {
            isArray = false;
            break;
          }
        }
        if (isArray) {
          var arr = [];
          var sortedKeys = keys.sort(function(a, b) { return parseInt(a, 10) - parseInt(b, 10); });
          for (var s = 0; s < sortedKeys.length; s++) {
            arr.push(value[sortedKeys[s]]);
          }
          obj[key] = arr;
        }
      }
    }
  }
});
