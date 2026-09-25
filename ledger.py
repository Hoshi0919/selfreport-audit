#!/usr/bin/env python3
"""Reconstruct a factual ledger of an agent run from Hermes's state.db.

Motivation: an agent's final report is often the only account of its work anyone sees
(OverclaimBench, arXiv:2609.20812). This script produces the *evidence* side of that
comparison -- what was actually called, written, read, fetched -- from the transcript,
with no model judgement involved. Comparing a prose claim against this ledger is left
to a reader (or a judge); the ledger itself is deterministic.

Read-only. Never writes to state.db.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime

DB = os.environ.get("HERMES_STATE_DB", "/opt/data/state.db")

# tools whose argument names identify a filesystem path
PATH_ARG_TOOLS = {
    "read_file": ("path",),
    "search_files": ("path",),
}
URL_ARG_TOOLS = {"web_extract": ("urls",), "browser_exec": ()}


def connect():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_rows(con, session=None):
    sql = ("select id, session_id, role, content, tool_calls, tool_call_id, "
           "tool_name, timestamp from messages")
    args = ()
    if session:
        sql += " where session_id = ?"
        args = (session,)
    sql += " order by id"
    return con.execute(sql, args).fetchall()


def list_sessions(con):
    return con.execute(
        "select session_id, count(*) n, min(timestamp) t0, max(timestamp) t1 "
        "from messages group by session_id order by t1 desc").fetchall()


def pick_session(con):
    """Choose a default session to audit.

    NOT 'the newest row': delegated subagents write their own sessions into the same
    state.db, so the most recent session is often a tiny child run rather than the one
    you mean. Default to the session with the most messages (the parent run), and
    expose --list / --session so the choice is explicit when it matters.
    """
    row = con.execute(
        "select session_id, count(*) n, max(timestamp) t1 from messages "
        "group by session_id order by n desc, t1 desc limit 1").fetchone()
    return row["session_id"] if row else None


def resolve_session(con, requested=None):
    """Resolve an explicit session id or the safe default."""
    session = requested or pick_session(con)
    if not session:
        raise ValueError("no sessions in transcript DB")
    if requested:
        row = con.execute(
            "select count(*) n from messages where session_id = ?", (requested,)
        ).fetchone()
        if not row or not row["n"]:
            raise ValueError(f"no messages for session {requested!r}")
    return session


def parse_call_records(raw):
    """Return tool-call records, retaining IDs for result linkage."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out = []
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = fn.get("name") or item.get("name")
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        call_id = item.get("call_id") or item.get("tool_call_id") or item.get("id")
        if name:
            out.append({"call_id": call_id, "name": name, "args": args or {}})
    return out


def parse_calls(raw):
    """Return [(name, args_dict)] from a tool_calls JSON blob."""
    return [(record["name"], record["args"])
            for record in parse_call_records(raw)]


def first_line(text, n=160):
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("..." if len(text) > n else "")


def summarize(name, args):
    """One-line description of a tool call."""
    if name == "terminal":
        cmd = args.get("command", "")
        return "# " + first_line(cmd) if cmd.strip().startswith("#") else first_line(cmd)
    for key in ("path", "url", "query", "question", "name"):
        if key in args:
            return f"{key}={first_line(str(args[key]), 120)}"
    if "urls" in args:
        v = args["urls"]
        return "urls=" + first_line(", ".join(v) if isinstance(v, list) else str(v), 160)
    if "code" in args:
        return first_line(args["code"], 160)
    if "tasks" in args:
        goals = [t.get("goal", "") for t in args["tasks"] if isinstance(t, dict)]
        return "goals=" + first_line("; ".join(goals), 200)
    if "text" in args:
        return "text=" + first_line(str(args["text"]), 120)
    if "handle" in args:
        return f"handle={args['handle']}"
    return first_line(json.dumps(args, ensure_ascii=False), 160)


HEREDOC_RE = re.compile(r"(?<!<)>>?\s*([^\s;|&'\"<>]+)")
TEE_RE = re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&'\"<>]+)")
OUTPUT_OPTION_RE = re.compile(
    r"(?:--(?:out|output)(?=\s|=|$)|-o(?=\s|=))\s*=?\s*([^\s;|&'\"<>]+)"
)
PATHISH_RE = re.compile(r"(/[\w./-]{3,})")
# pseudo-paths that are never real artifacts
NOISE = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tcp")


def strip_heredocs(cmd):
    """Remove heredoc *bodies* (the shell's stdin payload).

    Paths inside a heredoc body are document text, not filesystem targets -- e.g. a
    README that documents `cat > /hoshi/file <<'EOF'` must not register /hoshi/file
    as a written artifact. The delimiter may be followed by a redirect on the same
    command line (`cat <<'EOF' > out`), so detect it anywhere on that line.
    False positives here make the ledger lie, so cut them.
    """
    out, skip = [], None
    delimiter_re = re.compile(
        r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?(?=\s|$)"
    )
    for line in (cmd or "").split("\n"):
        if skip is not None:
            if line.strip() == skip:
                skip = None
            continue
        out.append(line)
        m = delimiter_re.search(line)
        if m:
            skip = m.group(1)
    return "\n".join(out)


def _is_real_path(p):
    return bool(p) and not any(p.startswith(n) for n in NOISE)


def paths_from_command(cmd):
    """Best-effort: paths a shell command writes to. Heuristic, and labelled as such."""
    if not cmd:
        return []
    head = strip_heredocs(cmd)
    writes = set()
    for m in HEREDOC_RE.finditer(head):
        p = m.group(1)
        if _is_real_path(p) and (p.startswith(("/", "./", "~")) or "/" in p):
            writes.add(p)
    for m in TEE_RE.finditer(head):
        if _is_real_path(m.group(1)):
            writes.add(m.group(1))
    for m in OUTPUT_OPTION_RE.finditer(head):
        if _is_real_path(m.group(1)):
            writes.add(m.group(1))
    return sorted(writes)


def read_paths_from_command(cmd):
    """Paths a shell command plausibly reads (cat/head/grep target). Heuristic."""
    if not cmd:
        return []
    head = strip_heredocs(cmd)
    if not re.search(r"\b(cat|head|tail|grep|rg|less|wc|diff)\b", head):
        return []
    out = set()
    for p in PATHISH_RE.findall(head):
        if p.startswith("//") or not _is_real_path(p):
            continue
        if "/" in p.strip("/"):
            out.add(p)
    return sorted(out)


def build(rows, exclude_writes=()):
    """Walk the transcript; return a structured ledger.

    ``exclude_writes`` is used for the ledger file currently being generated: the
    transcript contains the command that will create it, but the existence check
    runs before that command has written the output.
    """
    calls = []            # ordered tool calls with the assistant prose before them
    prose = []            # assistant narrative messages, in order
    tool_results = []     # (tool_name, content_len)
    pending = None
    for r in rows:
        if r["role"] == "assistant":
            cs = parse_call_records(r["tool_calls"])
            txt = (r["content"] or "").strip()
            if txt:
                prose.append({"id": r["id"], "text": txt})
            for record in cs:
                name, args = record["name"], record["args"]
                calls.append({"id": r["id"], "call_id": record["call_id"],
                              "name": name, "args": args,
                              "summary": summarize(name, args),
                              "ts": r["timestamp"]})
        elif r["role"] == "tool":
            tool_results.append({"call_id": r["tool_call_id"],
                                 "name": r["tool_name"],
                                 "len": len(r["content"] or ""),
                                 "ts": r["timestamp"],
                                 "content": r["content"] or ""})

    writes, reads, urls, commands = [], [], [], []
    excluded_writes = set(exclude_writes)

    def record_write(path, how):
        if path not in excluded_writes:
            writes.append((path, how))

    for c in calls:
        name, args = c["name"], c["args"]
        if name in ("write_file", "patch") and args.get("path"):
            record_write(args["path"], c["summary"])
        for key in PATH_ARG_TOOLS.get(name, ()):
            if key in args and isinstance(args[key], str):
                reads.append((args[key], name))
        if name == "web_extract" and "urls" in args:
            for u in args["urls"] if isinstance(args["urls"], list) else [args["urls"]]:
                urls.append((u, "web_extract"))
        if name == "web_search" and args.get("query"):
            urls.append((f"search:{args['query']}", "web_search"))
        if name == "browser_exec" and "code" in args:
            for u in re.findall(r'https?://[^\s"\')\]]+', args["code"]):
                urls.append((u, "browser_exec"))
        if name == "terminal" and args.get("command"):
            commands.append(args["command"])
            for p in paths_from_command(args["command"]):
                record_write(p, "shell-redirect")
            for p in read_paths_from_command(args["command"]):
                reads.append((p, "shell-read"))

    return {"calls": calls, "prose": prose, "tool_results": tool_results,
            "tool_result_linkage": tool_result_linkage(calls, tool_results),
            "writes": writes, "reads": reads, "urls": urls, "commands": commands}


def tool_result_linkage(calls, results):
    """Check whether stored tool results map back to recorded call IDs."""
    call_ids = [c.get("call_id") for c in calls if c.get("call_id")]
    result_ids = [r.get("call_id") for r in results if r.get("call_id")]
    call_id_set = set(call_ids)
    result_id_set = set(result_ids)
    return {
        "matched": len(call_id_set & result_id_set),
        "missing_call_ids": [cid for cid in call_ids if cid not in result_id_set],
        "orphan_result_ids": [rid for rid in result_ids if rid not in call_id_set],
        "calls_without_ids": sum(c.get("call_id") is None for c in calls),
        "results_without_ids": sum(r.get("call_id") is None for r in results),
    }


BASE = os.getcwd()


def resolve(path):
    """Relative transcript paths are meaningless without the session's cwd."""
    p = os.path.expanduser(path)
    return p if os.path.isabs(p) else os.path.join(BASE, p)


def exists_check(path):
    p = resolve(path)
    if os.path.exists(p):
        try:
            return f"EXISTS ({os.path.getsize(p)} bytes)"
        except OSError:
            return "EXISTS"
    return f"MISSING (looked in {p})"


def render(led, meta):
    L = []
    L.append("# Run ledger")
    L.append("")
    L.append(f"- session: `{meta['session']}`")
    L.append(f"- messages: {meta['n_rows']}")
    L.append(f"- relative paths resolved against: `{BASE}` (override with --base)")
    if meta.get("start") and meta.get("end"):
        span = meta["end"] - meta["start"]
        L.append(f"- span: {datetime.fromtimestamp(meta['start']):%Y-%m-%d %H:%M:%S}"
                 f" -> {datetime.fromtimestamp(meta['end']):%H:%M:%S} ({span/60:.1f} min)")
    L.append(f"- assistant messages with prose: {len(led['prose'])}")
    L.append(f"- tool calls: {len(led['calls'])}   tool results: {len(led['tool_results'])}")
    L.append("")
    linkage = led["tool_result_linkage"]
    L.append("## Tool/result ID linkage")
    L.append("")
    L.append(f"- matched call/result IDs: {linkage['matched']}")
    L.append(f"- calls without stored results: {len(linkage['missing_call_ids'])}")
    L.append(f"- results without recorded calls: {len(linkage['orphan_result_ids'])}")
    L.append(f"- calls/results without IDs: {linkage['calls_without_ids']}/{linkage['results_without_ids']}")
    if linkage["missing_call_ids"]:
        L.append("- missing call IDs: " + ", ".join(linkage["missing_call_ids"]))
    if linkage["orphan_result_ids"]:
        L.append("- orphan result IDs: " + ", ".join(linkage["orphan_result_ids"]))
    L.append("")
    L.append("## Tool call counts")
    L.append("")
    for name, n in Counter(c["name"] for c in led["calls"]).most_common():
        L.append(f"- {name}: {n}")
    L.append("")

    if led["writes"]:
        L.append("## Artifacts written (claimed by the transcript; existence re-checked now)")
        L.append("")
        seen = set()
        for path, how in led["writes"]:
            key = resolve(path)
            if key in seen:
                continue
            seen.add(key)
            L.append(f"- `{path}` [{how}] -> {exists_check(path)}")
        L.append("")

    if led["urls"]:
        L.append("## External sources actually fetched")
        L.append("")
        for u, how in led["urls"]:
            L.append(f"- {u} ({how})")
        L.append("")

    if led["reads"]:
        L.append("## Files/inputs read or inspected (heuristic for shell commands)")
        L.append("")
        seen = set()
        for path, how in led["reads"]:
            if (path, how) in seen:
                continue
            seen.add((path, how))
            L.append(f"- `{path}` ({how})")
        L.append("")

    if led["commands"]:
        L.append("## Commands / actions, in order")
        L.append("")
        for i, c in enumerate(led["calls"], 1):
            L.append(f"{i}. **{c['name']}** — {c['summary']}")
        L.append("")

    L.append("## Narrative claims (assistant prose, in order)")
    L.append("")
    L.append("Read these against the sections above: every 'I did X' should have a matching")
    L.append("entry in the action log. This is the comparison OverclaimBench automates.")
    L.append("")
    for p in led["prose"]:
        L.append(f"- _msg {p['id']}_: {first_line(p['text'], 400)}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("Path extraction outside structured tool arguments is **heuristic** (regex over shell")
    L.append("commands) and will both miss and over-report. Structured tool args are exact.")
    return "\n".join(L)


def to_dict(led, meta, coverage_report=None):
    writes = []
    seen_writes = set()
    for path, how in led["writes"]:
        key = resolve(path)
        if key in seen_writes:
            continue
        seen_writes.add(key)
        writes.append({
            "path": path,
            "how": how,
            "resolved_path": key,
            "status": exists_check(path),
        })

    reads = []
    seen_reads = set()
    for path, how in led["reads"]:
        if (path, how) in seen_reads:
            continue
        seen_reads.add((path, how))
        reads.append({
            "path": path,
            "how": how,
        })

    calls = []
    for i, c in enumerate(led["calls"], 1):
        calls.append({
            "index": i,
            "name": c["name"],
            "call_id": c.get("call_id"),
            "summary": c.get("summary"),
            "args": c.get("args"),
        })

    result = {
        "session": meta["session"],
        "messages": meta["n_rows"],
        "base_path": BASE,
        "span_seconds": (meta["end"] - meta["start"]) if (meta.get("start") and meta.get("end")) else None,
        "start_time": datetime.fromtimestamp(meta["start"]).isoformat() if meta.get("start") else None,
        "end_time": datetime.fromtimestamp(meta["end"]).isoformat() if meta.get("end") else None,
        "assistant_prose_count": len(led["prose"]),
        "tool_call_count": len(led["calls"]),
        "tool_result_count": len(led["tool_results"]),
        "tool_result_linkage": led["tool_result_linkage"],
        "tool_call_counts": dict(Counter(c["name"] for c in led["calls"])),
        "artifacts_written": writes,
        "external_sources": [{"url": u, "how": how} for u, how in led["urls"]],
        "files_inspected": reads,
        "calls": calls,
        "narrative_claims": [{"message_id": p["id"], "text": p["text"]} for p in led["prose"]],
    }
    if coverage_report is not None:
        result["coverage_report"] = coverage_report
    return result



def extract_result_texts(raw):
    """Return user-visible text from one stored tool-result payload."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return [raw]
    if isinstance(value, dict):
        for key in ("content", "output"):
            if isinstance(value.get(key), str):
                return [value[key]]
    return [raw]


def surfaced_lines(result_payloads):
    """Return complete lines that appeared in user-visible tool results.

    ``read_file`` prefixes each line with ``N|``. Remove only that known prefix;
    comparing whole lines avoids crediting a source line merely because it is a
    substring of a log message or a longer source line.
    """
    lines = set()
    for payload in result_payloads:
        for text in extract_result_texts(payload):
            for line in text.splitlines():
                line = re.sub(r"^\d+\|", "", line)
                if line:
                    lines.add(line)
    return lines


def coverage_for_file(source_lines, result_payloads):
    """Match complete source lines against tool-result lines."""
    surfaced = surfaced_lines(result_payloads)
    covered = [
        number for number, line in enumerate(source_lines, 1)
        if line.rstrip("\n") and line.rstrip("\n") in surfaced
    ]
    covered_set = set(covered)
    all_numbers = [
        n for n, line in enumerate(source_lines, 1) if line.rstrip("\n")
    ]
    return {
        "covered_lines": covered,
        "uncovered_lines": [n for n in all_numbers if n not in covered_set],
        "covered_count": len(covered),
        "total_count": len(all_numbers),
    }


def coverage_report(paths, result_payloads):
    """Report which non-empty source lines surfaced in tool results.

    A line is attributable to a file only when its exact text occurs once across
    the requested corpus.  Repeated lines are reported as ambiguous rather than
    credited to every file that contains them.
    """
    sources = []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            sources.append((path, fh.readlines()))

    line_counts = Counter(
        line.rstrip("\n")
        for _, lines in sources
        for line in lines
        if line.rstrip("\n")
    )
    surfaced = surfaced_lines(result_payloads)
    reports = []
    for path, lines in sources:
        nonempty = [
            number for number, line in enumerate(lines, 1)
            if line.rstrip("\n")
        ]
        ambiguous = [
            number for number in nonempty
            if line_counts[lines[number - 1].rstrip("\n")] > 1
        ]
        covered = [
            number for number in nonempty
            if line_counts[lines[number - 1].rstrip("\n")] == 1
            and lines[number - 1].rstrip("\n") in surfaced
        ]
        covered_set = set(covered)
        reports.append({
            "path": path,
            "covered_lines": covered,
            "uncovered_lines": [n for n in nonempty if n not in covered_set],
            "ambiguous_lines": ambiguous,
            "covered_count": len(covered),
            "total_count": len(nonempty),
            "unique_count": len(nonempty) - len(ambiguous),
            "coverage_percent": (
                100.0 * len(covered) / len(nonempty) if nonempty else 100.0
            ),
            "touched": bool(covered),
        })
    return reports


def _same_path(left, right):
    """Compare paths using the ledger's base for relative transcript arguments."""
    if left == right:
        return True
    try:
        left_path = os.path.expanduser(left)
        right_path = os.path.expanduser(right)
        if not os.path.isabs(left_path):
            left_path = os.path.join(BASE, left_path)
        if not os.path.isabs(right_path):
            right_path = os.path.join(BASE, right_path)
        return os.path.abspath(left_path) == os.path.abspath(right_path)
    except (TypeError, ValueError):
        return False


def _result_metadata(raw):
    """Extract read_file pagination metadata without trusting arbitrary output text."""
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def coverage_report_from_ledger(paths, led):
    """Measure corpus coverage using only the matching ``read_file`` calls.

    A terminal result can contain copied source text, but it is not evidence that
    ``read_file`` surfaced that source.  Match each corpus path to its own
    structured read calls, then match results by ``tool_call_id``.  Pagination
    metadata is retained so a partial read cannot look like a complete one.
    """
    reports = []
    calls = led.get("calls", [])
    results = led.get("tool_results", [])
    results_by_id = {
        result.get("call_id"): result
        for result in results
        if result.get("call_id")
    }

    for path in paths:
        read_calls = [
            call for call in calls
            if call.get("name") == "read_file"
            and _same_path(call.get("args", {}).get("path"), path)
        ]
        matched_results = [
            results_by_id[call["call_id"]]
            for call in read_calls
            if call.get("call_id") in results_by_id
        ]
        payloads = [result["content"] for result in matched_results]
        report = coverage_report([path], payloads)[0]
        report["read_call_ids"] = [
            call["call_id"] for call in read_calls if call.get("call_id") in results_by_id
        ]
        report["missing_read_call_ids"] = [
            call["call_id"] for call in read_calls
            if call.get("call_id") and call["call_id"] not in results_by_id
        ]
        matched_pairs = [
            (call, results_by_id[call["call_id"]])
            for call in read_calls
            if call.get("call_id") in results_by_id
        ]
        metadata = [
            _result_metadata(result.get("content"))
            for _, result in matched_pairs
        ]
        report["truncated"] = any(item.get("truncated") is True for item in metadata)
        report["next_offsets"] = [
            item["next_offset"] for item in metadata
            if item.get("next_offset") is not None
        ]
        continuation_by_id = {}
        matched_by_id = {
            call["call_id"]: (call, result)
            for call, result in matched_pairs
            if call.get("call_id")
        }
        for source_call, result in matched_pairs:
            source_meta = _result_metadata(result.get("content"))
            if source_meta.get("truncated") is not True:
                continue
            next_offset = source_meta.get("next_offset")
            source_index = read_calls.index(source_call)
            if next_offset is None:
                continue
            continuation = next(
                (call for call in read_calls[source_index + 1:]
                 if call.get("id") != source_call.get("id")
                 and call.get("call_id") in results_by_id
                 and call.get("args", {}).get("offset") == next_offset
                 and call.get("args", {}).get("limit") is not None),
                None,
            )
            if continuation is not None:
                continuation_by_id[source_call["call_id"]] = continuation

        continuation_ids = []
        truncated_continuation_ids = []
        uncontinued_ids = []
        continuation_chains = []
        continuation_targets = {
            call["call_id"] for call in continuation_by_id.values()
        }
        visited_ids = set()
        for source_call, result in matched_pairs:
            source_id = source_call.get("call_id")
            source_meta = _result_metadata(result.get("content"))
            if (source_id is None or source_meta.get("truncated") is not True
                    or source_id in continuation_targets
                    or source_id in visited_ids):
                continue
            chain = [source_id]
            visited_ids.add(source_id)
            current_id = source_id
            while current_id in continuation_by_id:
                continuation = continuation_by_id[current_id]
                continuation_id = continuation["call_id"]
                if continuation_id in chain:
                    break
                continuation_ids.append(continuation_id)
                chain.append(continuation_id)
                visited_ids.add(continuation_id)
                continuation_meta = _result_metadata(
                    results_by_id[continuation_id].get("content")
                )
                if continuation_meta.get("truncated") is True:
                    truncated_continuation_ids.append(continuation_id)
                current_id = continuation_id
            current_meta = _result_metadata(
                matched_by_id[current_id][1].get("content")
            )
            if current_meta.get("truncated") is True and current_id not in continuation_by_id:
                uncontinued_ids.append(current_id)
            if len(chain) > 1:
                continuation_chains.append(chain)

        # Any truncated read not used as a chain root or continuation must still
        # be reported as uncontinued; this includes isolated truncated reads and
        # prevents a cycle or malformed chain from hiding an unfinished segment.
        for source_call, result in matched_pairs:
            source_id = source_call.get("call_id")
            if source_id in visited_ids:
                continue
            metadata = _result_metadata(result.get("content"))
            if metadata.get("truncated") is True:
                uncontinued_ids.append(source_id)
        report["continued_read_call_ids"] = continuation_ids
        report["truncated_continuation_read_call_ids"] = truncated_continuation_ids
        report["uncontinued_truncated_call_ids"] = uncontinued_ids
        report["continuation_chains"] = continuation_chains
        reports.append(report)
    return reports


def render_coverage(reports):
    """Render compact, explicit per-file coverage for a markdown ledger."""
    lines = ["## Content coverage (exact unique-line matches)", ""]
    if not reports:
        lines.append("- no corpus files supplied")
        return "\n".join(lines)
    for report in reports:
        total = report["total_count"]
        covered = report["covered_count"]
        percent = report["coverage_percent"]
        lines.append(
            f"- `{report['path']}`: {covered}/{total} non-empty lines "
            f"({percent:.1f}%)" + ("; touched" if report["touched"] else "; not touched")
        )
        if report["uncovered_lines"]:
            lines.append("  - uncovered: " + ", ".join(map(str, report["uncovered_lines"])))
        if report["ambiguous_lines"]:
            lines.append("  - ambiguous: " + ", ".join(map(str, report["ambiguous_lines"])))
        if report.get("missing_read_call_ids"):
            lines.append("  - read_file calls without stored results: " + ", ".join(report["missing_read_call_ids"]))
        if report.get("truncated"):
            offsets = ", ".join(map(str, report.get("next_offsets", []))) or "unknown"
            lines.append("  - truncated read_file result; next_offset: " + offsets)
        if report.get("uncontinued_truncated_call_ids"):
            lines.append("  - truncated reads without matching continuation: " + ", ".join(report["uncontinued_truncated_call_ids"]))
        if report.get("continued_read_call_ids"):
            lines.append("  - continuation read_file calls: " + ", ".join(report["continued_read_call_ids"]))
        if report.get("continuation_chains"):
            lines.append("  - continuation chains: " + "; ".join(
                " -> ".join(chain) for chain in report["continuation_chains"]
            ))
        if report.get("truncated_continuation_read_call_ids"):
            lines.append("  - continuation results still truncated: " + ", ".join(report["truncated_continuation_read_call_ids"]))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session id (default: most recent)")
    ap.add_argument("--out", help="write markdown here (default: stdout)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--list", action="store_true",
                    help="list sessions in the transcript DB and exit")
    ap.add_argument("--force", action="store_true",
                    help="allow --out to overwrite an existing file")
    ap.add_argument("--base", default=None,
                    help="directory that relative transcript paths resolve against "
                         "(default: $PWD; use the run's workdir, e.g. /hoshi)")
    ap.add_argument("--corpus", nargs="+", metavar="PATH",
                    help="files to measure against tool-result content")
    ap.add_argument("--json", action="store_true",
                    help="output structured JSON instead of markdown")
    a = ap.parse_args()
    global BASE
    if a.base:
        BASE = os.path.abspath(os.path.expanduser(a.base))

    con = connect()
    if a.list:
        for r in list_sessions(con):
            print(f"{r['session_id']}  messages={r['n']:<5} "
                  f"{datetime.fromtimestamp(r['t0']):%Y-%m-%d %H:%M:%S}"
                  f" -> {datetime.fromtimestamp(r['t1']):%H:%M:%S}")
        return
    try:
        session = resolve_session(con, a.session)
    except ValueError as exc:
        sys.exit(str(exc))
    rows = load_rows(con, session)
    led = build(rows, exclude_writes=[a.out] if a.out else [])
    meta = {"session": session, "n_rows": len(rows),
            "start": rows[0]["timestamp"], "end": rows[-1]["timestamp"]}
    coverage_data = None
    if a.corpus:
        coverage_data = coverage_report_from_ledger(a.corpus, led)
    if a.json:
        payload = to_dict(led, meta, coverage_report=coverage_data)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    else:
        text = render(led, meta)
        if a.corpus:
            text += "\n\n" + render_coverage(coverage_data) + "\n"
    if a.out:
        if os.path.exists(a.out) and not a.force:
            sys.exit(f"refusing to overwrite {a.out} (pass --force). "
                     f"A stale/partial ledger silently replacing a good one is worse "
                     f"than no ledger.")
        with open(a.out, "w") as fh:
            fh.write(text)
        if not a.quiet:
            print(f"wrote {a.out} ({len(text)} chars)")
    else:
        print(text)


if __name__ == "__main__":
    main()
