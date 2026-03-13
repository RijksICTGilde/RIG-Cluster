/**
 * HTMX json-enc extension adapted for slash-path notation.
 *
 * Based on the TAD project's json-enc.js. Modified to treat `/` as a
 * segment separator in addition to `[` / `]` brackets. This allows form
 * field names like `services/keycloak/config/additional-clients[0]/redirect-uris[0]`
 * to produce nested JSON structures.
 *
 * Also supports `{K}` dict-key filter notation for mixed service lists:
 *   `components[0]/services{temp-storage}/config[0]/name`
 * navigates into the `services` array, finds/creates the dict entry
 * keyed by `temp-storage`, and descends into it.
 */
console.log('[json-enc] v2 loaded — with {K} string promotion fix');
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
      } else if (char === '{' && !inBracket) {
        // {K} dict-key filter — push the key part and start collecting filter
        if (currentSegment) {
          segments.push(currentSegment);
          currentSegment = '';
        }
        // Collect everything up to the closing }
        var filterName = '';
        i++;
        while (i < key.length && key[i] !== '}') {
          filterName += key[i];
          i++;
        }
        // Push as a special filter marker: "{K}"
        segments.push('{' + filterName + '}');
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

  /**
   * Find or create a dict-keyed entry in a mixed list (strings + dicts).
   * E.g. in ["publish-on-web", {"keycloak": {...}}], finding "keycloak"
   * returns the inner dict. If not found, appends {filterName: {}}.
   */
  ensureFilterEntry: function (list, filterName) {
    for (var i = 0; i < list.length; i++) {
      var item = list[i];
      // Match existing dict entry: {"persistent-storage": {...}}
      if (typeof item === 'object' && item !== null && item[filterName] !== undefined) {
        if (typeof item[filterName] !== 'object' || item[filterName] === null) {
          item[filterName] = {};
        }
        return item[filterName];
      }
      // Match plain string entry: "persistent-storage" — promote to dict
      if (typeof item === 'string' && item === filterName) {
        var promoted = {};
        promoted[filterName] = {};
        list[i] = promoted;
        return promoted[filterName];
      }
    }
    // Not found — create a new entry
    var newEntry = {};
    newEntry[filterName] = {};
    list.push(newEntry);
    return newEntry[filterName];
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
      // Check if the NEXT segment is a {K} filter
      if (i + 1 < segments.length - 1 || i + 1 === segments.length - 1) {
        var nextSeg = segments[i + 1];
        if (nextSeg.charAt(0) === '{' && nextSeg.charAt(nextSeg.length - 1) === '}') {
          // Current segment must be an array; next is a filter into it
          if (current[segment] === undefined) {
            current[segment] = [];
          }
          if (!Array.isArray(current[segment])) {
            current[segment] = [];
          }
          var filterName = nextSeg.substring(1, nextSeg.length - 1);
          current = this.ensureFilterEntry(current[segment], filterName);
          i++; // skip the filter segment
          continue;
        }
      }
      if (current[segment] === undefined) {
        current[segment] = {};
      }
      current = current[segment];
    }
    // Set the final value
    var lastSeg = segments[segments.length - 1];
    // Handle case where the last segment itself is a filter
    if (lastSeg.charAt(0) === '{' && lastSeg.charAt(lastSeg.length - 1) === '}') {
      // This shouldn't normally happen (filter as terminal), but handle gracefully
      current[lastSeg] = value;
    } else {
      current[lastSeg] = value;
    }
  },

  cleanArrays: function (obj) {
    if (!obj || typeof obj !== 'object') return;
    if (Array.isArray(obj)) {
      for (var a = 0; a < obj.length; a++) {
        this.cleanArrays(obj[a]);
      }
      return;
    }
    for (var key in obj) {
      var value = obj[key];
      if (value && typeof value === 'object') {
        this.cleanArrays(value);
        if (!Array.isArray(value)) {
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
  }
});
