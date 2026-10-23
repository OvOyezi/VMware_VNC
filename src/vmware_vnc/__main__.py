# SPDX-License-Identifier: Apache-2.0
# Modified for VMware VNC.
"""Run the same CLI through ``python -m vmware_vnc``."""
from .cli import main

raise SystemExit(main())
