"""Import/export organization rules from YAML (organize-rules.yml format)."""

import logging
from pathlib import Path

import yaml

from .. import database as db

log = logging.getLogger(__name__)


def import_rules_from_yaml(yaml_path: str) -> int:
    """Parse organize-rules.yml and insert rules into the DB.

    Expected YAML format:
        rules:
          - name: Photos
            extensions: [jpg, jpeg, png, gif, webp, heic]
            dest: Photos
          - name: Documents
            mime_prefix: [application/pdf, text/]
            dest: Documents
    """
    data = yaml.safe_load(Path(yaml_path).read_text())
    rules_list = data.get("rules", [])
    count = 0

    for r in rules_list:
        name = r.get("name", "Unnamed")
        if "extensions" in r:
            db.save_org_rule({
                "name": name,
                "description": f"Auto-imported: match extensions {r['extensions']}",
                "rule_type": "extension",
                "config_json": {
                    "extensions": r["extensions"],
                    "dest_folder": r.get("dest", name),
                },
                "priority": r.get("priority", 0),
                "enabled": True,
            })
            count += 1
        elif "mime_prefix" in r:
            db.save_org_rule({
                "name": name,
                "description": f"Auto-imported: match MIME {r['mime_prefix']}",
                "rule_type": "mime",
                "config_json": {
                    "patterns": r["mime_prefix"],
                    "dest_folder": r.get("dest", name),
                },
                "priority": r.get("priority", 0),
                "enabled": True,
            })
            count += 1
        elif "regex" in r:
            db.save_org_rule({
                "name": name,
                "description": f"Auto-imported: regex {r['regex']}",
                "rule_type": "regex",
                "config_json": {
                    "pattern": r["regex"],
                    "dest_folder": r.get("dest", name),
                },
                "priority": r.get("priority", 0),
                "enabled": True,
            })
            count += 1

    log.info("Imported %d rules from %s", count, yaml_path)
    return count


def export_rules_to_yaml(output_path: str) -> str:
    """Export DB rules back to YAML."""
    rules = db.get_org_rules()
    out = []
    for r in rules:
        entry = {"name": r["name"]}
        cfg = r["config_json"]
        if isinstance(cfg, str):
            import json
            cfg = json.loads(cfg)
        rtype = r["rule_type"]
        if rtype == "extension":
            entry["extensions"] = cfg.get("extensions", [])
            entry["dest"] = cfg.get("dest_folder", "")
        elif rtype == "mime":
            entry["mime_prefix"] = cfg.get("patterns", [])
            entry["dest"] = cfg.get("dest_folder", "")
        elif rtype == "regex":
            entry["regex"] = cfg.get("pattern", "")
            entry["dest"] = cfg.get("dest_folder", "")
        else:
            entry["type"] = rtype
            entry["config"] = cfg
        entry["priority"] = r.get("priority", 0)
        out.append(entry)

    content = yaml.dump({"rules": out}, default_flow_style=False)
    Path(output_path).write_text(content)
    return content
