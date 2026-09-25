#!/usr/bin/env python3
"""Print the node list from config.json as lines of "<name>\t<serial>".
Used by eanhost-up.sh / provision-node.sh so node count is data-driven.
"""
import json
import sys

cfg = {}
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except (IndexError, OSError, ValueError):
    pass

for serial, name in cfg.get("nodes", []):
    print(name + "\t" + serial)