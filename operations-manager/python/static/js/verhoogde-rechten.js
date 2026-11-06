/* Opens the popup from the account menu, and by itself after a redirect from an admin page
   (?verhoog=<path>). The server decides what goes in it. */
(function () {
    'use strict';

    function open(terug) {
        var dialoog = document.getElementById('verhoog-dialoog');
        htmx.ajax('GET', '/step-up/elevate?terug=' + encodeURIComponent(terug), {
            target: '#verhoog-inhoud',
            swap: 'innerHTML',
        }).then(function () {
            dialoog.show();
        });
    }

    document.addEventListener('click', function (event) {
        var pad = event.composedPath();
        function draagt(attribuut) {
            return pad.some(function (element) {
                return element.hasAttribute && element.hasAttribute(attribuut);
            });
        }
        if (draagt('data-verhoog-open')) {
            open(location.pathname + location.search);
        } else if (draagt('data-verhoog-sluit')) {
            document.getElementById('verhoog-dialoog').hide();
        }
    });

    var terug = new URLSearchParams(location.search).get('verhoog');
    if (terug) {
        history.replaceState(null, '', location.pathname);
        customElements.whenDefined('nldd-modal-dialog').then(function () {
            open(terug);
        });
    }
})();
