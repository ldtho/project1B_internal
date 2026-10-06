"""Browse the current training, validation and test data with all three annotation levels.

One page over the joint EgoVerse + EgoDex splits (data/splits): the video, the episode
instruction (level 1), the sub-task cues (level 2, EgoVerse only -- EgoDex episodes
are short and carry none) and the atomic hand-action lines (level 3), as a subtitle
track, a two-lane timeline and a nested list that follows playback.

  python -m data_prep.viz.datasets.server                       # defaults below, port 8324
  python -m data_prep.viz.datasets.server --source EgoDex=data/egodex/manifests/train.jsonl
  python -m data_prep.viz.datasets.server --source =data/splits/val.jsonl   # empty DATASET: the row's `dataset`
  python -m data_prep.viz.datasets.server --export out/          # manifests with the edits applied
  python -m data_prep.viz.datasets.server --selfcheck

Rows keep their own `split` (only KEEP_SPLITS are shown) and, in the split files, their own `dataset`.
Source videos stay unchanged; EgoVerse and clip playback use private H.264 copies.

Editing: the page's edit mode corrects the times and text of all three levels. A save
never touches a manifest; it appends one row to --edits (who, when, and the episode's
instruction / subtask / caption as saved) and the latest row per episode is applied on
load. A revert appends a row too, so the log is the full history. --export writes each
source manifest with the edits applied (pre-edit levels kept under `before_edit`).

QA: the EgoVerse episodes are dealt once among the people in QA_DIR/roster.json (--assign writes
QA_DIR/assignment.json; an admin takes half a reviewer's share). The page opens on a login: a
reviewer clicks their name, an admin also gives QA_ADMIN_PASSWORD (env, shared by the admins).
Saving needs that login; a reviewer saves only their own episodes, an admin any. A saved episode
is "corrected" (green) when its levels differ from the manifest, "confirmed" (orange) when saved unchanged.

  python -m data_prep.viz.datasets.server --assign               # once, after filling in the roster
  QA_ADMIN_PASSWORD=... python -m data_prep.viz.datasets.server
"""
from __future__ import annotations

import argparse
import functools
import fcntl
import hmac
import hashlib
import subprocess
import json
import os
import random
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
from data_prep.viz.corrector import inspect_server  # noqa: E402  (Range video serving, gzip JSON, WebVTT)
from data_prep.corrector.captions import assert_valid, fmt_cues, normalize_line, parse_cues as _parse_cues  # noqa: E402

UI = HERE / "index.html"
EDIT_LOG = "data/annotation_edits.jsonl"         # repo-relative, append-only
LEVELS = ("instruction", "subtask", "caption")   # the manifest fields an edit replaces
QA_DIR = "data/egoverse/qa"                      # roster.json {reviewers, admins}, assignment.json; committed
QA_DATASET = "EgoVerse"                          # the episodes --assign deals out

# (dataset, repo-relative manifest); each row's `split` field says train / val / test. An empty
# dataset takes each row's own `dataset` field (the joint split files mix both, data/splits/README.md).
SOURCES = [
    ("", "data/splits/train.jsonl"),
    ("", "data/splits/val.jsonl"),
    ("", "data/splits/test.jsonl"),
]
DATASET_NAMES = {"egoverse": "EgoVerse", "egodex": "EgoDex", "molmo": "Molmo", "abc130k": "ABC130K",
                 "galaxea": "Galaxea", "openaoe": "OpenAoE", "egocentric100k": "Egocentric100K",
                 "genhumanego": "GenHumanEgo"}
KEEP_SPLITS = ("train", "val", "test", "unsplit")
# The task / sub-task check runs (data/egoverse/corrector/full_pipeline.sh), merged by episode uid:
# <dir>/task_compare.jsonl has one row per episode (level 1), <dir>/compare.jsonl one per sub-task
# cue (level 2, same cue order as the row's `subtask`). Only what it judged wrong is shown. The
# EgoDex rows in data/splits already carry the fixed instruction (the old one is in `instruction_original`).
CHECKS = (
    "data/egoverse/corrector/train_v2",
    "data/egoverse/corrector/test_v2",
    "data/egodex/corrector/check_v2/train",
    "data/egodex/corrector/check_v2/test",
)
# The same video tree under each node's root (local SSD copy, sftp mirror); manifests carry either.
# A video is served from the first root of its group that exists here, the last one otherwise.
VIDEO_ROOTS = (
    ("/mnt/SSD4/dataset/egodex/EgoDex-LeRobot-v3.0", "/mnt/data/sftp/data/vla/pretrain/EgoDex-LeRobot-v3.0"),
    ("/mnt/SSD4/dataset/EgoVerse", "/mnt/data/sftp/data/vla/pretrain/egoverse-human-bimanual/episodes"),
)


@functools.cache
def local_root(group: tuple[str, ...]) -> str:
    return next((r for r in group if os.path.isdir(r)), group[-1])


def remap(path: str) -> str:
    for group in VIDEO_ROOTS:
        for old in group:
            if path.startswith(old + "/"):
                return local_root(group) + path[len(old):]
    return path


def owner(subs: list[dict], a: float, b: float) -> int:
    """Index of the sub-task cue an atomic line overlaps most, -1 when none."""
    best = max(((min(b, s["end"]) - max(a, s["start"]), s["i"]) for s in subs), default=(0.0, -1))
    return best[1] if best[0] > 0 else -1


def parse_cues(caption: str) -> list[tuple[float, float, str]]:
    """Keep native decimal precision; the training parser only accepts short timestamps."""
    if "-->" in caption:
        return _parse_cues(caption)
    cues = []
    for line in caption.strip().splitlines():
        match = re.fullmatch(r"\[([0-9]+(?:\.[0-9]+)?)\s*-\s*([0-9]+(?:\.[0-9]+)?)\]\s*(.+)", line.strip())
        cues.extend([(float(match[1]), float(match[2]), match[3])] if match else _parse_cues(line))
    return cues


def dataset_of(dataset: str, r: dict) -> str:
    """The source's dataset name, or the row's own one when the source leaves it empty."""
    return dataset or DATASET_NAMES.get(r.get("dataset"), r.get("dataset") or "?")


def eid(dataset: str, r: dict) -> str:
    return f"{dataset}:{r.get('split')}:{r['episode_uid']}"


def read_edits(path: Path) -> defaultdict[str, list[dict]]:
    out = defaultdict(list)
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                out[rec["id"]].append(rec)
    return out


def latest(recs: list[dict], source: str) -> dict | None:
    """The edit in force: the last record, unless it is a revert or was saved against
    another manifest file (a newer corrector run replaces the source, not the edit log)."""
    last = recs[-1] if recs else None
    return last if last and not last.get("revert") and last["source"] == source else None


def read_check(d: str) -> dict[str, dict]:
    """uid -> {"task": verdict, "subs": {cue index: verdict}} for what a check run judged wrong,
    a verdict being {error, old, new, saw} (saw = the blind description). A later row for the
    same key wins, since a retry appends."""
    out: dict[str, dict] = defaultdict(lambda: {"task": None, "subs": {}})
    for name in ("task_compare.jsonl", "compare.jsonl"):
        p = REPO / d / name
        for line in p.read_text().splitlines() if p.exists() else []:
            r = json.loads(line) if line.strip() else {}
            if r.get("status") != "ok":
                continue
            v = None if r["correct"] else {"error": r["error"], "old": r["text_old"], "new": r["text_new"],
                                            "saw": r.get("description"), "task_name": r.get("task_name")}
            if name == "task_compare.jsonl":
                out[r["episode_uid"]]["task"] = v
            else:
                out[r["episode_uid"]]["subs"][r["cue"]] = v
    return out


def flags(r: dict, check: dict | None) -> dict:
    """A row's check verdicts, ready to show: the task one with the suggested instruction as a
    whole line (its "name: " prefix kept when the row has one, as postprocess --task does), the
    cue ones pinned to their cue's times so they stay on the right line after a human edit."""
    check = check or {}
    task = check.get("task")
    if task:
        prefix = f"{task['task_name']}: "
        task = task | {"new": (prefix if (r.get("instruction") or "").startswith(prefix) else "") + task["new"]}
    cues = parse_cues(r.get("subtask") or "")
    subs = [v | {"start": cues[k][0], "end": cues[k][1]}
            for k, v in sorted((check.get("subs") or {}).items()) if v and k < len(cues)]
    return {"task": task, "subs": subs}


def qa_status(r: dict, live: dict | None) -> str | None:
    """"corrected" when the edit in force differs from the manifest row, "confirmed" when it was saved
    unchanged, None before any save or after a revert. The row goes through the same clean_edit a save
    does, so formatting alone never counts as a correction; a row that fails it was corrected by any save."""
    if not live:
        return None
    if live.get("crop"):
        return "corrected"
    cue = lambda v, k: [{"start": a, "end": b, "text": t} for a, b, t in parse_cues(v.get(k) or "")]  # noqa: E731
    def cleaned(v):
        return clean_edit({"duration": float(r["duration_s"]), "row": r},
                          {"instruction": v.get("instruction"), "subtasks": cue(v, "subtask"), "atomic": cue(v, "caption")})
    try:
        original, current = cleaned(r), cleaned(live)
        same = {k: original[k] for k in LEVELS} == {k: current[k] for k in LEVELS}
    except ValueError:
        same = False
    return "confirmed" if same else "corrected"


def duration_stats(cues: list[dict]) -> dict:
    durations = [c["end"] - c["start"] for c in cues]
    return {"total": sum(durations), "min": min(durations, default=None), "max": max(durations, default=None)}


def episode(dataset: str, path: str, r: dict, recs: list[dict] = (), check: dict | None = None,
            assignee: str | None = None) -> dict:
    """One manifest row (plus its edit history, check verdicts and QA assignee) -> the viewer's episode."""
    live = latest(recs, path)
    v = r | {k: live[k] for k in LEVELS} if live else r
    chk = flags(r, check)
    source_events = r.get("review_events") if not live else None
    sub_cues = ((e["start"], e["end"], e["caption"]) for e in source_events if e["level"] == "L2") \
        if source_events is not None else parse_cues(v.get("subtask") or "")
    subs = [{"i": k, "start": a, "end": b, "desc": t} for k, (a, b, t) in enumerate(sub_cues)]
    for s in subs:   # a verdict stays on the cue whose times it was given for
        s["check"] = next((f for f in chk["subs"] if abs(f["start"] - s["start"]) < 1e-6
                           and abs(f["end"] - s["end"]) < 1e-6), None)
    fallback = set(v.get("split_fallback_idx") or []) if not live else set()   # sub-task cues kept unsplit
    atomic = []
    atomic_cues = ((e["start"], e["end"], e["caption"]) for e in source_events if e["level"] == "L3.5") \
        if source_events is not None else parse_cues(v["caption"])
    for a, b, t in atomic_cues:
        k = owner(subs, a, b)
        atomic.append({"start": a, "end": b, "text": t, "cue": k, **({"fallback": True} if k in fallback else {})})
    if r.get("review_events") and not live:
        for level, track in (("L2", subs), ("L3.5", atomic)):
            events = [e for e in r["review_events"] if e["level"] == level]
            assert len(events) == len(track)
            for cue, event in zip(track, events):
                cue.update({k: event[k] for k in ("annotation_id", "split", "selection_status", "discard_reason",
                                                 "selection_reason", "source_splits")})
    levels = [level for level, cues in (("L2", subs), ("L3.5", atomic)) if cues]
    tags = (["partial split"] if v.get("split_status") == "partial" and not live else [])
    if chk["task"] or chk["subs"]:
        tags.append("annotation check")
    if r.get("review_reasons"):
        tags.append("review notes")
    tags.extend(k.replace("_", " ") for k, count in (r.get("source_issues") or {}).items() if count)
    if live and live.get("review_flag"):
        tags.append("flagged for review")
    return {"id": eid(dataset, r), "uid": r["episode_uid"],
            "dataset": dataset, "split": r["split"], "task": r.get("task_group") or r.get("task", ""),
            "instruction": v.get("instruction", ""), "duration": float(r["duration_s"]),
            "video": remap(r["video"]), "bucket": r.get("bucket", ""),
            **{k: r.get(k) for k in ("scene", "selection_status", "discard_reason",
                                    "selection_reason", "format_status", "source_splits", "resolution",
                                    "actual_fps", "source_duration_s", "original_caption", "full_recording",
                                    "split_memberships", "review_groups", "review_reasons", "subtask_source", "source_tracks")},
            "annotation_levels": levels, "annotation_level": " + ".join(levels), "flags": tags,
            "clip_start": r.get("start") if r.get("timestamp_origin") == "clip" else None,
            "clip_end": r.get("end") if r.get("timestamp_origin") == "clip" else None,
            "partial": v.get("split_status") == "partial" and not live, "source": path,
            "subtasks": subs, "atomic": atomic, "row": r, "version": len(recs),
            "sub_durations": duration_stats(subs), "atomic_durations": duration_stats(atomic),
            "edited": {"editor": live["editor"], "time": live["time"]} if live else None,
            "qa": qa_status(r, live), "assignee": assignee,
            "review_flag": bool(live and live.get("review_flag")), "review_reason": live.get("review_reason", "") if live else "",
            "crop": live.get("crop") if live else None,
            "check": chk, "n_flag": bool(chk["task"]) + len(chk["subs"])}


def selected_recording(r: dict) -> dict | None:
    """Keep full playback, but expose only selected annotation events."""
    if "review_events" not in r:
        return r
    events = [e for e in r["review_events"] if e["selection_status"] == "chosen"]
    if not events:
        return None
    counts = Counter((e["split"], e["level"]) for e in events)
    memberships = sorted({e["split"] for e in events})
    levels = sorted({e["level"] for e in events})
    captions = lambda level: "\n".join(f"[{e['start']} - {e['end']}] {e['caption']}" for e in events if e["level"] == level)
    return r | {"review_events": events, "split": memberships[0], "split_memberships": memberships,
                "review_groups": [{"split": split, "level": level, "status": "chosen", "reason": "", "count": n}
                                  for (split, level), n in sorted(counts.items())],
                "annotation_levels": levels, "annotation_level": " + ".join(levels),
                "selection_status": "chosen", "discard_reason": "",
                "selection_reason": "Selected annotations only",
                "source_splits": sorted({split for e in events for split in e["source_splits"]}),
                "caption": captions("L3.5"), "subtask": captions("L2"),
                "original_caption": captions("L2") + "\n" + captions("L3.5")}


def pantheon_annotations(r: dict, native: dict) -> dict:
    """Expose downloaded timed labels, retaining each source track and its original bounds."""
    tracks = []
    for key, name, start, end, text in (("dataset_labels", "Dataset labels", "t0", "t1", "label"),
                                       ("tasks", "Pantheon task segments", "start_s", "end_s", "task")):
        if native.get(key):
            tracks.append({"name": name, "key": key,
                           "note": native.get("dataset_labels_note") if key == "dataset_labels" else
                                   "Task segments may overlap or cover a whole episode.",
                           "cues": [{"start": e[start], "end": e[end], "text": e[text]} for e in native[key]]})
    preferred = "tasks" if r["dataset"] in ("openaoe", "egocentric100k") else "dataset_labels"
    track = next((t for t in tracks if t["key"] == preferred), None)
    update = {"source_tracks": tracks}
    if track and not r.get("subtask"):
        duration = float(r["duration_s"])
        missing = [c for c in track["cues"] if c["text"].startswith("(task index ")
                   and "missing from the dataset's task list" in c["text"]]
        cues = [(max(0, c["start"]), min(duration, c["end"]), c["text"]) for c in track["cues"] if c not in missing]
        subtask = "\n".join(f"[{a:.3f} - {b:.3f}] {text}" for a, b, text in sorted(cues) if a < b)
        update |= {"subtask": subtask, "subtask_source": track["name"] if subtask else None}
        if missing:
            update["source_issues"] = (r.get("source_issues") or {}) | {"missing_subtask_labels": len(missing)}
    return r | update


def prepare_row(r: dict) -> dict | None:
    r = selected_recording(r)
    if r is not None and r["episode_uid"].startswith("pantheon/"):
        native = json.loads(Path(r["source_annotation"]).read_text())
        r = pantheon_annotations(r, native)
        if r.get("split") is None:
            r = r | {"split": "unsplit", "task": " / ".join(native.get("task_label") or []) or r["instruction"],
                     "original_caption": r.get("caption_original", ""),
                     "format_status": "Format corrected; human review pending"}
    return r


def load(sources: list[tuple[str, str]], edits: dict[str, list[dict]] | None = None,
         checks: dict[str, dict] | None = None, assigned: dict[str, str] | None = None) -> list[dict]:
    eps = []
    for dataset, path in sources:
        for line in (REPO / path).read_text().splitlines():
            if not line.strip():
                continue
            r = prepare_row(json.loads(line))
            if r is None:
                continue
            if r.get("split") not in KEEP_SPLITS:
                continue
            ds = dataset_of(dataset, r)
            eps.append(episode(ds, path, r, (edits or {}).get(eid(ds, r), []), (checks or {}).get(r["episode_uid"]),
                               (assigned or {}).get(eid(ds, r))))
    return eps


def assign(ids: list[str], reviewers: list[str], admins: list[str], seed: int = 0) -> dict[str, str]:
    """Episode ids -> who reviews them, dealt round-robin after a seeded shuffle: a reviewer takes two
    seats per round, an admin one, so an admin ends up with half a reviewer's share (give or take one)."""
    ids = sorted(ids)
    random.Random(seed).shuffle(ids)
    seats = reviewers * 2 + admins
    return {i: seats[k % len(seats)] for k, i in enumerate(ids)}


def read_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _valid(cues, duration) -> bool:
    try:
        assert_valid(cues, duration)
        return True
    except AssertionError:
        return False


def format_caption(text: str) -> str:
    """Normalize caption formatting without changing actions or assigning hand labels."""
    text = " ".join(str(text or "").split())
    names = {"left": "left hand", "right": "right hand", "both": "both hands", "ego": "ego"}
    text = re.sub(r"\[(?:(left|right|both) hands?|ego)\]\s*",
                  lambda m: f"[{names[(m[1] or 'ego').lower()]}] ", text, flags=re.I)
    parts = re.split(r"\s*\|+\s*|(?=\[(?:left hand|right hand|both hands|ego)\])", text)
    parts = [part.strip().rstrip(". ") for part in parts]
    return " | ".join(part for part in parts if part)


def clean_edit(e: dict, body: dict) -> dict:
    """The POSTed levels -> the manifest fields they replace; ValueError names the first problem.
    Times snap to 0.1 s, the caption format's resolution. Sub-tasks stay inside the video and
    do not overlap (gaps are fine). Atomic lines keep the training invariants -- contiguous from
    0 to the end of the video, canonical hand labels -- when the manifest row met them; a row
    that never did (EgoDex test still carries [ego]) is held to the timing ones only.
    Native review events retain gaps, overlaps, decimal precision and absent instructions."""
    dur = e["duration"]
    review_flag = body.get("review_flag", e.get("review_flag", False))
    if type(review_flag) is not bool:
        raise ValueError("Review flag must be a boolean")
    review_reason = body.get("review_reason", e.get("review_reason", ""))
    if not isinstance(review_reason, str) or len(review_reason) > 2000:
        raise ValueError("Review reason must be text of at most 2000 characters")
    review_reason = review_reason.strip() if review_flag else ""
    if review_flag and not review_reason:
        raise ValueError("A reason is required when flagging for review")
    crop = body.get("crop", e.get("crop"))
    if crop is not None:
        if not isinstance(crop, dict) or set(crop) != {"start", "end"} or any(type(crop[k]) not in (int, float) for k in crop):
            raise ValueError("Crop requires numeric start and end")
        a, b = crop["start"], crop["end"]
        if not 0 <= a < b <= dur or b - a < 0.1 - 1e-9:
            raise ValueError("Crop must keep at least 0.1 s inside the video")
        crop = {"start": float(a), "end": float(b)}
        if a == 0 and b == dur:
            crop = None
    native_events = e["row"].get("review_events")
    ins = format_caption(body.get("instruction"))
    if not ins and not (native_events and not e["row"].get("instruction")):
        raise ValueError("the instruction is empty")

    def cues(items, level):
        out = []
        precision = 3 if level == "sub-task" and e["row"].get("subtask_source") else 1
        overlap = native_events or (level == "sub-task" and e["row"].get("subtask_source") == "Pantheon task segments")
        source_level = "L2" if level == "sub-task" else "L3.5"
        source_times = {(c["start"], c["end"]) for c in native_events or [] if c["level"] == source_level}
        for k, c in enumerate(items or []):
            a, b = float(c["start"]), float(c["end"])
            if not native_events:
                a, b = round(a, precision), round(b, precision)
            t = format_caption(c.get("text"))
            where = f"{level} {k + 1}"
            if not t:
                raise ValueError(f"{where}: empty text")
            existing_boundary = (a, b) in source_times and 0 <= a < b <= dur + 0.05
            if not 0 <= a < b <= dur + 1e-6 and not existing_boundary:
                raise ValueError(f"{where}: {a:.1f}-{b:.1f} s is empty or outside the video (0-{dur:.1f} s)")
            if out and (a < out[-1][0] - 1e-6 or (not overlap and a < out[-1][1] - 1e-6)):
                raise ValueError(f"{where} starts before {level} {k} ends")
            out.append((a, b, t))
        return out

    subs = cues(body.get("subtasks"), "sub-task")
    atoms = cues(body.get("atomic"), "atomic line")
    strict = not native_events and _valid(parse_cues(e["row"]["caption"]), dur)
    if strict:
        atoms = [(a, b, normalize_line(t)) for a, b, t in atoms]
    if native_events:
        if not atoms:
            raise ValueError("atomic lines: empty caption")
    else:
        try:
            assert_valid(atoms if strict else [(a, b, "[both hands] x") for a, b, _ in atoms], dur)
        except AssertionError as err:
            raise ValueError(f"atomic lines: {str(err).replace('cue ', 'line ')}") from None
    native_cues = lambda track: "\n".join(f"[{a} - {b}] {text}" for a, b, text in track)
    if native_events:
        subtask = native_cues(subs)
    elif e["row"].get("subtask_source"):
        subtask = "\n".join(f"[{a:.3f} - {b:.3f}] {text}" for a, b, text in subs)
    else:
        subtask = fmt_cues(subs)
    return {"instruction": ins, "subtask": subtask, "caption": native_cues(atoms) if native_events else fmt_cues(atoms),
            "review_flag": review_flag, "review_reason": review_reason, "crop": crop}


def export_path(path: str) -> Path:
    relative = Path(path)
    if '..' in relative.parts:
        raise ValueError('Export source must not contain parent traversal')
    return Path('external') / relative.relative_to(relative.anchor) if relative.is_absolute() else relative


def export(sources: list[tuple[str, str]], edits: dict[str, list[dict]], out_dir: Path,
           source_files: dict[str, Path] | None = None) -> None:
    """Apply edits using the same row preparation and IDs as playback; contain all outputs."""
    for dataset, path in sources:
        rows, n = [], 0
        source = source_files[path] if source_files is not None else REPO / path
        for line in source.read_text().splitlines():
            if not line.strip():
                continue
            r = prepare_row(json.loads(line))
            if r is None:
                continue
            live = latest(edits.get(eid(dataset_of(dataset, r), r), []), path)
            if live:
                r = r | {k: live[k] for k in LEVELS} | {"before_edit": {k: r.get(k, "") for k in LEVELS},
                                                         "edited_by": live["editor"], "edited_at": live["time"],
                                                         "edited_by_id": live.get("editor_id"),
                                                         "edit_version": live.get("version", len(edits[eid(dataset_of(dataset, r), r)]))}
                r |= {"review_flag": live.get("review_flag", False), "review_reason": live.get("review_reason", ""), "video_crop": live.get("crop"),
                      "video_crop_timestamp_origin": "episode", "training_ready": not live.get("review_flag", False)}
                n += 1
            rows.append(json.dumps(r))
        out = out_dir / export_path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(rows) + "\n")
        print(f"{out}: {n} of {len(rows)} rows edited")


TRANSCODE_SLOTS = threading.Semaphore(2)
CLIP_CACHE = REPO / "data/viz_clips"


def video_info(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=codec_name,width,height,avg_frame_rate,start_time,duration,nb_frames",
                             "-of", "json", str(path)], check=True, capture_output=True, timeout=30)
    return json.loads(result.stdout)["streams"][0]


def playback_file(e: dict) -> Path:
    """Private H.264 playback copies; source videos and caption timestamps stay unchanged."""
    source = Path(e["video"])
    full_egoverse = e.get("clip_start") is None and e.get("dataset", "").lower() == "egoverse"
    if e.get("clip_start") is None and not full_egoverse:
        return source
    a, b = (None, None) if full_egoverse else (float(e["clip_start"]), float(e["clip_end"]))
    if not full_egoverse and not 0 <= a < b:
        raise ValueError("Invalid playback clip bounds")
    stat = source.stat()
    profile = "egoverse-h264-v1" if full_egoverse else "clip-480-v1"
    key = hashlib.sha256(f"{source}:{stat.st_mtime_ns}:{stat.st_size}:{a}:{b}:{profile}".encode()).hexdigest()
    destination = CLIP_CACHE / f"{key}.mp4"
    if destination.exists():
        return destination
    CLIP_CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Per-file lock also coordinates preloading with requests from the running service.
    with destination.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not destination.exists():
            temporary = destination.with_suffix(".tmp.mp4")
            try:
                with TRANSCODE_SLOTS:
                    original = video_info(source) if full_egoverse else None
                    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", "-filter_threads", "2"]
                    if not full_egoverse:
                        command += ["-ss", str(a)]
                    command += ["-i", str(source)]
                    command += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy", "-fps_mode", "passthrough"] \
                        if full_egoverse else ["-t", str(b-a), "-an", "-vf", "scale=480:-2"]
                    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                                "-crf", "23" if full_egoverse else "25", "-g", "60", "-threads", "2",
                                "-movflags", "+faststart", str(temporary)]
                    subprocess.run(command, check=True, timeout=180,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    if full_egoverse:
                        converted = video_info(temporary)
                        if converted["codec_name"] != "h264" or any(original[k] != converted[k] for k in
                                ("width", "height", "avg_frame_rate", "nb_frames")) or any(
                                abs(float(original[k]) - float(converted[k])) > 0.001 for k in ("start_time", "duration")):
                            raise OSError("Playback conversion changed video frames or timing")
                    current = source.stat()
                    if (stat.st_mtime_ns, stat.st_size) != (current.st_mtime_ns, current.st_size):
                        raise OSError("Source video changed during playback conversion")
                    temporary.chmod(0o600)
                    temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
    return destination


def haystack(e: dict) -> str:
    """What the search box matches: every level's text plus task and uid."""
    return " ".join([e["task"], e["instruction"], e["uid"], *(s["desc"] for s in e["subtasks"]),
                     *(a["text"] for a in e["atomic"]),
                     *(c["text"] for track in e.get("source_tracks") or [] for c in track["cues"]),
                     *(str(e.get(k) or "") for k in ("scene", "annotation_level", "selection_status", "discard_reason"))]).lower()


def summary(e: dict) -> dict:
    """The list-row view: everything but the per-line annotations."""
    return {k: v for k, v in e.items() if k not in ("subtasks", "atomic", "video", "source", "row", "check", "original_caption", "format_status", "review_reasons", "source_tracks")} | \
        {"n_sub": len(e["subtasks"]), "n_atomic": len(e["atomic"])}


def caption_state(e: dict) -> dict:
    return {"instruction": e["instruction"],
            "review_flag": e.get("review_flag", False), "crop": e.get("crop"),
            "review_reason": e.get("review_reason", ""),
            "subtasks": [{"start": s["start"], "end": s["end"], "text": s["desc"]} for s in e["subtasks"]],
            "atomic": [{k: a[k] for k in ("start", "end", "text")} for a in e["atomic"]]}


def original_captions(e: dict, recs: list[dict], source_sha256: str | None = None) -> dict:
    for rec in recs:
        if rec.get("original") and rec["source"] == e["source"] and \
                (not source_sha256 or rec.get("source_sha256") == source_sha256):
            return rec["original"]
    return caption_state(episode(e["dataset"], e["source"], e["row"]))


def detail(e: dict, recs: list[dict] = (), source_sha256: str | None = None) -> dict:
    recs = recs[:e["version"]]
    original = original_captions(e, recs, source_sha256)
    history = []
    previous = original
    for version, rec in enumerate(recs, 1):
        same_source = rec["source"] == e["source"] and \
            (not source_sha256 or not rec.get("source_sha256") or rec["source_sha256"] == source_sha256)
        entry = {"version": version, "editor": rec["editor"], "editor_id": rec.get("editor_id"),
                 "time": rec["time"], "revert": bool(rec.get("revert")),
                 "earlier_source": not same_source,
                 "available": same_source or bool(rec.get("original") and rec.get("before"))}
        if entry["available"]:
            entry["original"] = rec.get("original", original)
            entry["before"] = rec.get("before", previous)
            entry["captions"] = entry["original"] if rec.get("revert") else {
                "instruction": rec["instruction"],
                "review_flag": rec.get("review_flag", False), "crop": rec.get("crop"),
                "review_reason": rec.get("review_reason", ""),
                "subtasks": [{"start": a, "end": b, "text": text} for a, b, text in parse_cues(rec["subtask"])],
                "atomic": [{"start": a, "end": b, "text": text} for a, b, text in parse_cues(rec["caption"])]}
            previous = entry["captions"] if same_source else original
        else:
            previous = original
        history.append(entry)
    return {k: v for k, v in e.items() if k not in ("video", "row")} | {"original": original, "history": history}


class Handler(inspect_server.Handler):
    hay: dict[str, str]
    checks: dict[str, dict] = {}   # uid -> check verdicts
    edits: defaultdict[str, list[dict]]
    edit_log: Path
    lock = threading.Lock()   # version check, log append and episode swap happen as one
    roster: dict[str, list[str]] = {"reviewers": [], "admins": []}
    assigned: dict[str, str] = {}   # episode id -> name
    key = os.urandom(32)            # signs login tokens; a restart logs everyone out

    def token(self, name: str) -> str:
        """name (hex, so any script survives JSON and headers) + its HMAC: a login the server can check statelessly."""
        return name.encode().hex() + "." + hmac.new(self.key, name.encode(), "sha256").hexdigest()

    def user(self, tok) -> tuple[str, str] | None:
        """A login token -> (name, "admin" | "reviewer"), None when forged, stale or off the roster."""
        try:
            name = bytes.fromhex(str(tok).partition(".")[0]).decode()
        except ValueError:
            return None
        if not hmac.compare_digest(str(tok), self.token(name)):
            return None
        return (name, "admin") if name in self.roster["admins"] else \
            (name, "reviewer") if name in self.roster["reviewers"] else None

    def login(self, body: dict) -> dict:
        """{token} re-checks a stored login; {name[, password]} makes one. PermissionError says why not."""
        if body.get("token"):
            u = self.user(body["token"])
            if not u:
                raise PermissionError("the login expired, pick your name again")
            name, role = u
        else:
            name = str(body.get("name") or "")
            if name in self.roster["admins"]:
                pw = os.environ.get("QA_ADMIN_PASSWORD") or ""
                if not pw:
                    raise PermissionError("admin login is off: start the server with QA_ADMIN_PASSWORD set")
                if not hmac.compare_digest(str(body.get("password") or "").encode(), pw.encode()):
                    raise PermissionError("wrong password")
                role = "admin"
            elif name in self.roster["reviewers"]:
                role = "reviewer"
            else:
                raise PermissionError("not on the reviewer list")
        return {"name": name, "role": role, "token": self.token(name)}

    def do_GET(self):
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        if u.path == "/api/episodes":
            return self._json({"episodes": [summary(e) for e in self.by_uid.values()], "roster": self.roster})
        if u.path == "/api/search":   # all words, anywhere in the episode's three levels
            words = (q.get("q") or [""])[0].lower().split()
            return self._json({"ids": [i for i, h in self.hay.items() if all(w in h for w in words)]})
        if u.path in ("/api/episode", "/vtt"):
            e = self._episode(q)
            if e is None:
                return None
            if u.path == "/vtt":
                return self._send(200, inspect_server.to_vtt(e["subtasks"], e["atomic"]).encode(),
                                  "text/vtt; charset=utf-8")
            return self._json(detail(e, self.edits.get(e["id"], []), getattr(self, "source_hashes", {}).get(e["source"])))
        if u.path == "/video":
            e = self._episode(q)
            if e is None:
                return None
            try:
                return self._serve_video(playback_file(e))
            except (OSError, subprocess.SubprocessError):
                return self._send(503, b"Could not prepare this video; retry or inspect server log", "text/plain")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        """/api/login {name, password?} or {token} -> {name, role, token}; /api/edit {token, id, version,
        instruction, subtasks, atomic} or /api/revert {token, id, version} -> the updated episode. The
        editor is the login's name. `version` is the number of log rows the editor's copy was built
        from: a save on top of someone else's newer one is a 409."""
        u = urlsplit(self.path)
        if u.path not in ("/api/login", "/api/edit", "/api/revert"):
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1 << 20:
            return self._json({"error": "request too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/login":
                return self._json(self.login(body))
            who = self.user(body.get("token"))
            if not who:
                return self._json({"error": "log in first (pick your name)"}, 401)
            editor, role = who
            with self.lock:
                e = self.by_uid.get(body.get("id"))
                if e is None:
                    return self._json({"error": "unknown episode"}, 404)
                if role != "admin" and e["assignee"] != editor:
                    return self._json({"error": f"assigned to {e['assignee'] or 'nobody'}: only they or an admin "
                                                f"can save it"}, 403)
                recs = self.edits[e["id"]]
                if body.get("version") != len(recs):
                    last = recs[-1] if recs else {}
                    return self._json({"error": f"{last.get('editor', 'someone')} saved this episode at "
                                                f"{last.get('time', '?')} after you opened it -- reload it first"}, 409)
                rec = {"id": e["id"], "source": e["source"], "uid": e["uid"], "editor": editor,
                       "time": time.strftime("%Y-%m-%d %H:%M:%S")}
                rec |= {"revert": True} if u.path == "/api/revert" else clean_edit(e, body)
                with self.edit_log.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")
                recs.append(rec)
                e = self.by_uid[e["id"]] = episode(e["dataset"], e["source"], e["row"], recs,
                                                   self.checks.get(e["uid"]), e["assignee"])
                self.hay[e["id"]] = haystack(e)
        except PermissionError as err:
            return self._json({"error": str(err)}, 401)
        except (ValueError, KeyError, TypeError) as err:
            return self._json({"error": str(err)}, 400)
        return self._json(detail(e, recs))


def selfcheck() -> None:
    import tempfile
    with tempfile.TemporaryDirectory(dir=REPO) as d:
        rel = Path(d).relative_to(REPO)
        (Path(d) / "a.jsonl").write_text("\n".join(json.dumps(r) for r in [
            {"episode_uid": "egoverse/e1", "task_group": "do_dishes", "instruction": "Do dishes", "split": "train",
             "duration_s": "6.0", "video": "/mnt/SSD4/dataset/EgoVerse/e1/images.front_1.mp4",
             "subtask": "00:00.0 - 00:04.0: Wash the plate\n00:04.0 - 00:06.0: Dry the plate",
             "caption": "00:00.0 - 00:01.5: [right hand] pick up the plate\n"
                        "00:01.5 - 00:04.0: [both hands] scrub the plate\n00:04.0 - 00:06.0: [both hands] Dry the plate",
             "split_status": "partial", "split_fallback_idx": [1]},
            {"episode_uid": "egoverse/e2", "split": "val", "duration_s": 1, "video": "/x.mp4",
             "caption": "00:00.0 - 00:01.0: x"},
            {"episode_uid": "t/ep1", "dataset": "egodex", "task": "t_v2.1", "instruction": "T: do", "split": "test", "subtask": "",
             "duration_s": 2, "video": "/x.mp4", "caption": "00:00.0 - 00:02.0: [left hand] grasp cup"},
        ]) + "\n")
        v, w, x = load([("EgoVerse", f"{rel}/a.jsonl")])   # a named source wins over the row's `dataset`
        mixed = load([("", f"{rel}/a.jsonl")])                # an empty one takes it
        chosen = {"level": "L3.5", "annotation_id": "keep", "start": 1.12345678, "end": 2.7,
                  "caption": "pick up cup", "split": "val", "selection_status": "chosen",
                  "source_splits": ["val"], "discard_reason": "", "selection_reason": ""}
        discard = chosen | {"annotation_id": "drop", "selection_status": "discarded", "split": "train"}
        context = chosen | {"annotation_id": "test", "selection_status": "context", "split": "test"}
        original = {"episode_uid": "recording", "split": "train", "duration_s": 80, "video": "/full.mp4",
                    "full_recording": True, "review_events": [discard, chosen, context]}
        selected = selected_recording(original)
        assert selected["review_events"] == [chosen] and original["review_events"] == [discard, chosen, context]
        assert selected["split_memberships"] == ["val"] and selected["duration_s"] == 80
        assert selected["annotation_levels"] == ["L3.5"] and selected_recording(original | {"review_events": [discard]}) is None
        assert episode("EgoExoLearn", "fixture", selected)["atomic"][0]["start"] == 1.12345678
        selected_ep = episode("EgoExoLearn", "fixture", selected)
        assert selected_ep["annotation_levels"] == ["L3.5"]
        assert selected_ep["annotation_level"] == "L3.5"
        (Path(d) / "native.json").write_text(json.dumps({"task_label": ["Fold clothes"]}))
        (Path(d) / "pantheon.jsonl").write_text(json.dumps({"episode_uid": "pantheon/molmo/test", "dataset": "molmo",
            "duration_s": 2, "video": "/main.mp4", "caption": "[0.0 - 2.0] [left hand] grasp cup",
            "caption_original": "[0.0 - 2.0] [left hand] Grasp cup", "instruction": "Fold clothes",
            "source_annotation": str(Path(d) / "native.json"), "train_eligible": False}) + "\n")
        public = load([("", f"{rel}/pantheon.jsonl")])[0]
        assert public["dataset"] == "Molmo" and public["split"] == "unsplit" and public["task"] == "Fold clothes"
        assert not public["row"]["train_eligible"] and public["atomic"][0]["end"] == 2
        assert public["annotation_levels"] == ["L3.5"] and public["flags"] == []
        native = {"dataset_labels": [{"t0": 0.123, "t1": 1.333, "label": "Lift cup"},
                                      {"t0": 1.333, "t1": 2.468, "label": "Place cup"}],
                  "tasks": [{"start_s": 0, "end_s": 1.5, "task": "Move cup"},
                            {"start_s": 1, "end_s": 2, "task": "Arrange cup"}]}
        for ds, source in (("galaxea", "Dataset labels"), ("genhumanego", "Dataset labels"),
                           ("openaoe", "Pantheon task segments"), ("egocentric100k", "Pantheon task segments")):
            imported = episode(ds, "fixture", pantheon_annotations(public["row"] | {"dataset": ds}, native))
            assert imported["annotation_levels"] == ["L2", "L3.5"] and imported["subtask_source"] == source
            assert len(imported["subtasks"]) == 2 and len(imported["source_tracks"]) == 2
            assert "source_tracks" not in summary(imported)
            assert "arrange cup" in haystack(imported) and "place cup" in haystack(imported)
            body = {"instruction": imported["instruction"], "subtasks": [{"start": c["start"], "end": c["end"], "text": c["desc"]}
                    for c in imported["subtasks"]], "atomic": imported["atomic"]}
            assert parse_cues(clean_edit(imported, body)["subtask"]) == [(c["start"], c["end"], c["desc"]) for c in imported["subtasks"]]
            if source == "Dataset labels":
                assert imported["subtasks"][0]["end"] == 1.333 and imported["subtasks"][-1]["end"] == 2
                assert imported["source_tracks"][0]["cues"][-1]["end"] == 2.468
        assert pantheon_annotations(public["row"] | {"subtask": "[0.0 - 2.0] Existing caption"}, native)["subtask"] \
            == "[0.0 - 2.0] Existing caption"
        (Path(d) / "native.json").write_text(json.dumps(native))
        (Path(d) / "pantheon.jsonl").write_text(json.dumps(public["row"] | {"dataset": "galaxea", "split": "train"}) + "\n")
        assert load([("", f"{rel}/pantheon.jsonl")])[0]["subtasks"][0]["desc"] == "Lift cup"
        short = episode("galaxea", "fixture", pantheon_annotations(public["row"] | {"dataset": "galaxea", "duration_s": 1.333},
                        {"dataset_labels": [{"t0": 1.301, "t1": 1.468, "label": "Release cup"}]}))
        body = {"instruction": short["instruction"], "subtasks": [{"start": 1.301, "end": 1.333, "text": "Release cup"}],
                "atomic": [{"start": 0, "end": 1.3, "text": "[left hand] grasp cup"}]}
        assert parse_cues(clean_edit(short, body)["subtask"]) == [(1.301, 1.333, "Release cup")]
        missing = episode("galaxea", "fixture", pantheon_annotations(public["row"] | {"dataset": "galaxea"},
                          {"dataset_labels": [{"t0": 0, "t1": 2, "label": "(task index 7, missing from the dataset's task list)"}]}))
        assert missing["annotation_levels"] == ["L3.5"] and missing["subtask_source"] is None
        assert "missing subtask labels" in missing["flags"] and len(missing["source_tracks"][0]["cues"]) == 1
    here = local_root(VIDEO_ROOTS[1]) + "/e1/images.front_1.mp4"   # either root -> this node's one
    assert v["video"] == here == remap(VIDEO_ROOTS[1][1] + "/e1/images.front_1.mp4"), v
    assert local_root(("/nonexistent/a", "/nonexistent/b")) == "/nonexistent/b"
    assert [s["desc"] for s in v["subtasks"]] == ["Wash the plate", "Dry the plate"], v["subtasks"]
    assert v["annotation_levels"] == ["L2", "L3.5"] and v["flags"] == ["partial split"]
    l2 = episode("Any dataset", "fixture", x["row"] | {"subtask": "[0.0 - 2.0] Pick up cup", "caption": "",
                 "annotation_level": "L3.5", "review_reasons": ["Inspect wording"], "source_issues": {"overlap_conflict": 1}})
    assert l2["annotation_levels"] == ["L2"] and l2["annotation_level"] == "L2"
    assert l2["flags"] == ["review notes", "overlap conflict"]
    assert "review_reasons" not in summary(l2) and detail(l2)["review_reasons"] == ["Inspect wording"]
    assert [a["cue"] for a in v["atomic"]] == [0, 0, 1] and [a.get("fallback") for a in v["atomic"]] == [None, None, True]
    assert v["partial"] and v["task"] == "do_dishes"
    assert x["subtasks"] == [] and x["atomic"][0]["cue"] == -1 and x["id"] == "EgoVerse:test:t/ep1"
    assert w["split"] == "val" and [m["dataset"] for m in mixed] == ["?", "?", "EgoDex"] and mixed[2]["id"] == "EgoDex:test:t/ep1"
    assert owner([{"i": 0, "start": 0, "end": 1}, {"i": 1, "start": 1, "end": 3}], 0.8, 2.0) == 1
    s = summary(v)
    assert s["n_sub"] == 2 and s["n_atomic"] == 3 and not {"atomic", "video", "source"} & set(s)
    assert s["sub_durations"] == {"total": 6, "min": 2, "max": 4}
    assert s["atomic_durations"] == {"total": 6, "min": 1.5, "max": 2.5}
    assert summary(x)["sub_durations"] == {"total": 0, "min": None, "max": None}
    assert detail(v)["sub_durations"] == s["sub_durations"]
    assert "wash the plate" in haystack(v) and "scrub" in haystack(v) and "egoverse/e1" in haystack(v)
    check_edits()
    check_flags()
    print("selfcheck ok")


def check_flags() -> None:
    """read_check -> flags -> episode: task prefix kept, cue verdicts follow the cue's times."""
    import tempfile
    row = {"episode_uid": "egoverse/e1", "instruction": "Wash: Do dishes", "split": "train", "duration_s": 6,
           "video": "/x.mp4", "subtask": "[0.0 - 4.0] Wash the plate\n[4.0 - 6.0] Dry the plate",
           "caption": "[0.0 - 4.0] [both hands] scrub the plate\n[4.0 - 6.0] [right hand] dry the plate"}
    with tempfile.TemporaryDirectory(dir=REPO) as d:
        rel = str(Path(d).relative_to(REPO))
        ok = {"episode_uid": "egoverse/e1", "status": "ok", "description": "rinse a cup"}
        (Path(d) / "task_compare.jsonl").write_text(json.dumps(ok | {
            "correct": False, "error": "object", "text_old": "Do dishes", "text_new": "Rinse a cup", "task_name": "Wash"}) + "\n")
        (Path(d) / "compare.jsonl").write_text("\n".join(json.dumps(ok | x) for x in [
            {"cue": 0, "correct": False, "error": "object", "text_old": "Wash the plate", "text_new": "Wash the cup"},
            {"cue": 1, "correct": False, "error": "action", "text_old": "Dry the plate", "text_new": "x"},
            {"cue": 1, "correct": True, "error": "none", "text_old": "Dry the plate", "text_new": "Dry the plate"},  # retry wins
            {"cue": 0, "status": "error"}]) + "\n")
        check = read_check(rel)["egoverse/e1"]
    e = episode("EgoVerse", "m.jsonl", row, (), check)
    assert e["check"]["task"]["new"] == "Wash: Rinse a cup" and e["n_flag"] == 2, e["check"]
    assert e["subtasks"][0]["check"]["new"] == "Wash the cup" and e["subtasks"][1]["check"] is None, e["subtasks"]
    moved = [{"id": eid("EgoVerse", row), "source": "m.jsonl", "editor": "a", "time": "t", "instruction": "Do dishes",
              "subtask": "[0.0 - 3.0] Wash the cup\n[3.0 - 6.0] Dry the plate", "caption": row["caption"]}]
    e = episode("EgoVerse", "m.jsonl", row, moved, check)
    assert e["subtasks"][0]["check"] is None and e["n_flag"] == 2, e   # times changed: not pinned, still counted
    assert "check" not in summary(e) and summary(e)["n_flag"] == 2
    print("check flags ok: task prefix, retry wins, verdicts pinned to cue times")


def check_edits() -> None:
    """clean_edit rules, then save / conflict / revert / export through a live server."""
    import tempfile
    import urllib.error
    import urllib.request
    row = {"episode_uid": "egoverse/e1", "task_group": "do_dishes", "instruction": "Do dishes", "split": "train",
           "duration_s": 6.03, "video": "/x.mp4", "subtask": "00:00.0 - 00:04.0: Wash the plate",
           "caption": "00:00.0 - 00:04.0: [both hands] scrub the plate\n00:04.0 - 00:06.0: [right hand] dry the plate"}
    e = episode("EgoVerse", "m.jsonl", row)
    ok = {"instruction": " Wash  dishes ", "subtasks": [{"start": 0, "end": 3.04, "text": "Wash the plate"}],
          "atomic": [{"start": 0, "end": 2.96, "text": "[BOTH HANDS] scrub the plate"},
                     {"start": 2.96, "end": 6.0, "text": "[right hand] dry the plate"}]}
    got = clean_edit(e, ok)
    assert got["instruction"] == "Wash dishes" and parse_cues(got["subtask"]) == [(0.0, 3.0, "Wash the plate")], got
    assert parse_cues(got["caption"]) == [(0.0, 3.0, "[both hands] scrub the plate"),
                                          (3.0, 6.0, "[right hand] dry the plate")], got   # snapped + normalized
    for bad, why in ((ok | {"instruction": " "}, "instruction"),
                     (ok | {"subtasks": [{"start": 0, "end": 3, "text": "a"}, {"start": 2, "end": 4, "text": "b"}]}, "before"),
                     (ok | {"atomic": [{"start": 0, "end": 3, "text": "[left hand] a"}, {"start": 3.5, "end": 6, "text": "[left hand] b"}]}, "contiguous"),
                     (ok | {"atomic": [{"start": 0, "end": 5, "text": "[left hand] a"}]}, "last end"),
                     (ok | {"atomic": [{"start": 0, "end": 7, "text": "[left hand] a"}]}, "outside")):
        try:
            clean_edit(e, bad)
            raise AssertionError(f"accepted: {bad}")
        except ValueError as err:
            assert why in str(err), (why, err)
    ego = episode("EgoDex", "t.jsonl", row | {"caption": "00:00.0 - 00:06.0: [ego] look | [left hand] hold cup"})
    kept = clean_edit(ego, ok | {"atomic": [{"start": 0, "end": 6, "text": "[ego] look | [left hand] hold cup"}]})
    assert parse_cues(kept["caption"])[0][2] == "[ego] look | [left hand] hold cup", kept   # never canonical: text kept

    with tempfile.TemporaryDirectory(dir=REPO) as d:
        rel = Path(d).relative_to(REPO)
        (Path(d) / "m.jsonl").write_text(json.dumps(row) + "\n")
        sources = [("EgoVerse", f"{rel}/m.jsonl")]
        Handler.edit_log = Path(d) / "edits.jsonl"
        Handler.edits = read_edits(Handler.edit_log)
        Handler.roster = {"reviewers": ["ann", "bob"], "admins": ["boss"]}
        Handler.assigned = {"EgoVerse:train:egoverse/e1": "ann"}
        Handler.by_uid = {x["id"]: x for x in load(sources, Handler.edits, None, Handler.assigned)}
        Handler.hay = {i: haystack(x) for i, x in Handler.by_uid.items()}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        def login(name, password=None):
            code, j = post("/api/login", {"name": name, "password": password})
            return j.get("token") if code == 200 else code

        ann, bob = login("ann"), login("bob")
        assert login("eve") == 401 and post("/api/login", {"token": ann})[1]["name"] == "ann"
        assert post("/api/login", {"token": bob[:-1] + "0"})[0] == 401             # forged signature
        base = {"id": "EgoVerse:train:egoverse/e1", "token": ann}
        assert post("/api/edit", base | ok | {"version": 0, "token": None})[0] == 401
        assert post("/api/edit", base | ok | {"version": 0, "token": bob})[0] == 403   # not bob's episode
        code, j = post("/api/edit", base | ok | {"version": 0})
        assert code == 200 and j["version"] == 1 and j["edited"]["editor"] == "ann" and j["instruction"] == "Wash dishes", j
        assert j["qa"] == "corrected" and j["assignee"] == "ann", j
        assert post("/api/edit", base | ok | {"version": 0})[0] == 409            # built on the pre-save copy
        assert "wash dishes" in Handler.hay[base["id"]]
        # a restart replays the log
        again = load(sources, read_edits(Handler.edit_log))[0]
        assert again["instruction"] == "Wash dishes" and again["version"] == 1 and len(again["atomic"]) == 2
        export(sources, read_edits(Handler.edit_log), Path(d) / "out")
        out = json.loads((Path(d) / "out" / rel / "m.jsonl").read_text())
        assert out["before_edit"]["instruction"] == "Do dishes" and out["edited_by"] == "ann", out
        assert parse_cues(out["caption"])[0][1] == 3.0, out
        # an admin needs the password, may save anyone's episode; the manifest's own levels saved back = confirmed
        pw = os.environ.pop("QA_ADMIN_PASSWORD", None)
        try:
            assert login("boss", "pw") == 401                                      # admin login off without the env
            os.environ["QA_ADMIN_PASSWORD"] = "pw"
            assert login("boss", "nope") == 401 and login("boss") == 401
            same = {"instruction": "Do dishes", "subtasks": [{"start": 0, "end": 4, "text": "Wash the plate"}],
                    "atomic": [{"start": 0, "end": 4, "text": "[both hands] scrub the plate"},
                               {"start": 4, "end": 6, "text": "[right hand] dry the plate"}]}
            code, j = post("/api/edit", base | same | {"version": 1, "token": login("boss", "pw")})
            assert code == 200 and j["qa"] == "confirmed" and j["edited"]["editor"] == "boss", j
        finally:
            os.environ.pop("QA_ADMIN_PASSWORD")
            if pw is not None:
                os.environ["QA_ADMIN_PASSWORD"] = pw
        code, j = post("/api/revert", base | {"version": 2})
        assert code == 200 and j["edited"] is None and j["qa"] is None and j["instruction"] == "Do dishes" and j["version"] == 3, j
        srv.shutdown()
    deal = assign([f"e{k}" for k in range(60)], ["a", "b"], ["x", "y"])
    assert Counter(deal.values()) == {"a": 20, "b": 20, "x": 10, "y": 10}, Counter(deal.values())
    assert deal == assign([f"e{k}" for k in reversed(range(60))], ["a", "b"], ["x", "y"])   # order-free, seeded
    print("edit checks ok: login, assignee-only saves, admin password, validation, 409, replay, export, "
          "corrected / confirmed, revert, deal")


def write_assignment(out: Path, eps: list[dict], roster: dict[str, list[str]]) -> None:
    """The QA deal, written once: a second run refuses, so a review in progress keeps its split."""
    if out.exists():
        sys.exit(f"{out} exists; the assignment is fixed once dealt (delete it to deal again)")
    if not roster["reviewers"] and not roster["admins"]:
        sys.exit(f"{out.parent}/roster.json lists nobody")
    pool = {e["id"]: e for e in eps if e["dataset"] == QA_DATASET}
    got = assign(list(pool), roster["reviewers"], roster["admins"])
    out.write_text(json.dumps({"dataset": QA_DATASET, "seed": 0, "roster": roster, "episodes": dict(sorted(got.items()))},
                              indent=1) + "\n")
    print(f"{out}: {len(got)} {QA_DATASET} episodes")
    for name in roster["reviewers"] + roster["admins"]:
        mine = [pool[i] for i, n in got.items() if n == name]
        print(f"  {name:20s} {'admin' if name in roster['admins'] else 'reviewer':8s} {len(mine):5d} episodes "
              f"{sum(e['duration'] for e in mine) / 3600:5.1f} h")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", action="append", metavar="DATASET=MANIFEST",
                    help="repo-relative manifest; repeat. Replaces the defaults when given. "
                         "An empty DATASET (=MANIFEST) takes each row's `dataset`")
    ap.add_argument("--port", type=int, default=8324)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--edits", default=EDIT_LOG, help=f"edit log, repo-relative (default {EDIT_LOG})")
    ap.add_argument("--export", metavar="DIR", help="write the source manifests with the edits applied under DIR, then exit")
    ap.add_argument("--assign", action="store_true",
                    help=f"deal the {QA_DATASET} episodes among {QA_DIR}/roster.json into {QA_DIR}/assignment.json, then exit")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows up under nohup
    if args.selfcheck:
        return selfcheck()
    sources = [tuple(s.split("=", 1)) for s in args.source] if args.source else SOURCES
    Handler.edit_log = REPO / args.edits
    Handler.edits = read_edits(Handler.edit_log)
    if args.export:
        return export(sources, Handler.edits, Path(args.export))
    qa = REPO / QA_DIR
    Handler.roster = read_json(qa / "roster.json", Handler.roster)
    if args.assign:
        return write_assignment(qa / "assignment.json", load(sources), Handler.roster)
    Handler.assigned = read_json(qa / "assignment.json", {}).get("episodes", {})
    if off := sorted(set(Handler.assigned.values()) - set(Handler.roster["reviewers"] + Handler.roster["admins"])):
        print(f"warning: {QA_DIR}/assignment.json names people no longer on the roster: {', '.join(off)}")
    Handler.checks = {uid: v for d in CHECKS for uid, v in read_check(d).items()}
    eps = load(sources, Handler.edits, Handler.checks, Handler.assigned)
    counts = Counter(f"{e['dataset']} {e['split']}" for e in eps)
    print(f"{len(eps)} episodes: " + ", ".join(f"{k} {n}" for k, n in counts.items())
          + f"; {sum(bool(e['edited']) for e in eps)} edited ({Handler.edit_log})"
          + f"; check flags: {sum(bool(e['check']['task']) for e in eps)} tasks, "
            f"{sum(len(e['check']['subs']) for e in eps)} sub-tasks")
    Handler.by_uid = {e["id"]: e for e in eps}
    Handler.hay = {e["id"]: haystack(e) for e in eps}
    print(f"http://localhost:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
