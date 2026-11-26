"""检查合成资料的交换格式、业务标识与来源引用。"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import unittest


SAMPLES = Path(__file__).resolve().parents[1] / "samples"
SCHEMA = json.loads((SAMPLES / "formats.json").read_text(encoding="utf-8"))
TYPES = {"string": str, "int": int, "bool": bool, "list": list, "time": str, "date": str}


def read_feed(name):
    return [json.loads(line) for line in (SAMPLES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]


def select(row, fields):
    return tuple(row[field] for field in fields)


class SourceRecordsTest(unittest.TestCase):
    def test_declared_feeds_are_readable(self):
        self.assertEqual(
            set(SCHEMA["feeds"]),
            {path.stem for path in SAMPLES.glob("*.jsonl")},
        )
        for name, spec in SCHEMA["feeds"].items():
            with self.subTest(feed=name):
                records = read_feed(name)
                self.assertTrue(records)
                for record in records:
                    self.assertEqual(record.keys(), spec["fields"].keys())
                    for field, kind in spec["fields"].items():
                        value = record[field]
                        self.assertIs(type(value), TYPES[kind], (name, field))
                        if kind == "time":
                            self.assertTrue(value.endswith("Z"))
                            parsed = datetime.fromisoformat(value)
                            self.assertEqual(parsed.tzinfo, timezone.utc)
                        elif kind == "date":
                            self.assertEqual(date.fromisoformat(value).isoformat(), value)
                identifiers = [select(record, spec["key"]) for record in records]
                self.assertTrue(all(all(value != "" for value in key) for key in identifiers))
                self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_source_references_exist(self):
        for source, keys, target, attributes in SCHEMA["references"]:
            with self.subTest(source=source, target=target, keys=keys):
                expected = {select(record, attributes) for record in read_feed(target)}
                for record in read_feed(source):
                    self.assertIn(select(record, keys), expected)
