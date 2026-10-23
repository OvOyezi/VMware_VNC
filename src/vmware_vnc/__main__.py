# SPDX-License-Identifier: Apache-2.0
# Modified and maintained for VMware VNC; historical attribution is in NOTICE.
"""Run the same CLI through ``python -m vmware_vnc``."""
from .cli import main

raise SystemExit(main())
