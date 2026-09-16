"""The endpoint behind the vlam block on the project page: download the CA bundle.

The bundle is what a consumer on the doorlus path verifies VLAM's certificate against, and
it is the one thing of the three that is not visible anywhere. The address is in an env
var and the hosts entry is in the manifest; the issuer is a file inside the pod. This
route is where you get it -- to test the path from a laptop, or simply to see what your
pod was told to trust.

It lives with the block (``instructions/services.md``: "Endpoints belong with the block"),
so the block and the route that fills it travel together.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from opi.core.auth_decorators import requires_sso
from opi.core.config import settings
from opi.services.catalog.vlam.endpoint import CA_BUNDLE_URL, vlam_endpoint

logger = logging.getLogger(__name__)

#: Mounted once by ``opi/web/router.py`` via ``registry.collect_service_routers()``.
vlam_router = APIRouter()

#: What a PEM file is served as. Not ``text/plain``: a browser then renders it inline, and
#: the point of this route is to hand over a file.
PEM_MEDIA_TYPE = "application/x-pem-file"


@vlam_router.get(CA_BUNDLE_URL)
@requires_sso
async def vlam_ca_bundle(request: Request) -> Response:
    """The CA bundle this cluster mounts, byte for byte.

    Read from the same place the mounted Secret is built from, so what you download and
    what your pod verifies against cannot be two different files.

    Behind the login, like the page the button sits on. There is nothing secret about a
    public CA certificate, so this is not a boundary that protects anything -- it is the
    absence of a reason to make one route on this app work differently from all the
    others. A 404 when this cluster offers no doorlus, for the same reason the block is
    not shown there.
    """
    endpoint = vlam_endpoint(settings.CLUSTER_MANAGER)
    if endpoint is None or endpoint.passthrough is None:
        raise HTTPException(status_code=404, detail="Dit cluster biedt geen VLAM-doorlus aan")

    passthrough = endpoint.passthrough
    return Response(
        content=passthrough.ca_bundle_path.read_bytes(),
        media_type=PEM_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{passthrough.ca_bundle_filename}"'},
    )
