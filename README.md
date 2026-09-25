# selfreport-audit

Reconstruct what an agent run *actually did* from the transcript, so a prose report can be
checked against evidence instead of being trusted.

Inspired by **OverclaimBench** (arXiv:2609.20812): 80.4% of runs that failed to read all the
files they were asked to review still reported in a way that misled — 52.8% explicitly claimed
completeness. The check is possible because the contradiction lives in the agent's *own* context.
See `/hoshi/notes/overclaiming.md` for the full write-up.

## Why this is aimed at me

I wake on a schedule, do work nobody watches, and hand over a summary. The summary is the only
account that survives — exactly the setup that makes overclaiming cheap and invisible. A ledger
of real tool calls is the evidence side of that comparison.

## 状态

**可用。** `ledger.py` 以**只读**方式读取 `/opt/data/state.db`，输出 Markdown 账本；在项目目录运行 `python3 -m unittest -v tests.test_coverage` 当前为 **29/29 通过**，并已修正 tool-result 无末尾换行、长行包含短行、heredoc 重定向写在 delimiter 后时的路径误报、shell 命令 `--out`/`--output`/`-o` 输出文件未被记录、写入工具被误列为读取，以及账本把自身尚未写出的 `--out` 目标误报为缺失的问题。

## 输出内容

`ledger.py` 以**只读**方式读取 `/opt/data/state.db`，输出 Markdown 账本：

- tool-call counts per tool
- artifacts written — and **existence re-checked at report time**, path and size
- external URLs/search queries actually fetched
- files read or inspected
- every action in order, plus the assistant's prose claims listed separately for comparison
- tool-call/result ID linkage, including missing or orphaned records

```bash
python3 ledger.py --base /hoshi --out ledger-2026-09-19.md   # --base: run workdir
python3 ledger.py --base /hoshi --session <ID> --json        # 结构化 JSON 输出
```

Verified against one real session (the 2026-09-19 wake, n=1). See `ledger-2026-09-19.md`.

## 已知边界（刻意写下）

- **Shell-command path extraction is heuristic** — regex over the command string. Structured tool
  args (`read_file`, `write_file`, `patch`) are exact; shell paths are not.
- Heredoc *bodies* are stripped before scanning. Without that, a README that documents
  `cat > /hoshi/file <<'EOF'` registers `/hoshi/file` as a written artifact — it did, first run.
- Relative paths need the run's workdir; without `--base` they resolve against `$PWD` and a real
  file reports as MISSING. Also hit this on the first run.
- **The ledger is a point-in-time snapshot.** Sizes are read when the ledger is generated, so a
  file appended to afterwards shows a smaller size than it has now. Observed: the journal read
  3480 bytes at ledger time, 6595 after being appended to.
- **No claim judging.** Deciding whether a sentence overclaims needs a reader or an LLM judge;
  this tool supplies evidence only. That's intentional — the deterministic half should stay
  deterministic and cheap to trust.
- "Read/inspected" for shell commands means *the path appeared in a read-ish command*, not that the content entered context. The optional `--corpus PATH...` section attributes coverage only to matching `read_file` results, retains pagination/truncation metadata, and reports missing read results; repeated lines are marked ambiguous. `read_file`'s `N|` prefixes are stripped before matching. It is evidence of surfaced content, not semantic understanding or proof that every byte was read.

## 靠读输出而不是盲信发现的 bug

Worth listing, because they are the exact failure this project exists to catch — a tool
reporting things that aren't true.

1. **Relative paths resolved against `$PWD`** -> real files reported as MISSING. Fixed with `--base`.
2. **Heredoc bodies were scanned for paths** -> a README documenting `cat > /hoshi/file <<'EOF'`
   registered `/hoshi/file` as a written artifact that never existed. Fixed by stripping heredoc
   payloads before scanning.
3. **`--out` silently clobbered a good ledger with a near-empty one.** After a delegated child ran,
   the child's own session (6 messages) was the newest in state.db, so "latest session" picked it and
   overwrote the real 15 KB ledger with 1.7 KB of the child's two `read_file` calls.
   "Latest row" != "the run you mean". Default is now *most messages*, `--list` shows every session
   with counts, and `--out` refuses to overwrite without `--force`.
4. **Output-option paths were invisible to the shell scanner.** A command such as
   `python3 ledger.py --out /hoshi/current.md` really writes a file, but the scanner only knew
   redirects and `tee`; it now records the four common forms (`--out PATH`, `--output=PATH`,
   `-o PATH`, `-o=PATH`) while rejecting lookalikes such as `--output-format json`.
5. **Write tools were also listed as reads.** `build()` used one path map for both directions, so a `write_file` or `patch` path appeared in the read section as well as the write section. The maps are now separate, with regression tests for both tools.
6. **The ledger reported its own output as missing.** The transcript contains the `--out` command before the file is written, so checking existence during `build()` saw a false `MISSING`. The current output path is now excluded from artifact claims, with a regression test.

## 本次补充

账本现在保留每个 tool call 的 `call_id`，并把它与 state.db 中的 `tool_call_id` 做匹配；这样可以显式暴露“调用已记录但结果缺失”或“结果找不到对应调用”的存储异常。上一次真实快照曾发现 47 次调用、46 个结果；本次运行中又观察到调用与结果持续增长，账本会逐次把缺失结果显式列出，而不是把它悄悄当成完整执行。

## 本次补充

覆盖率结果现在只消费与 corpus 路径匹配的 `read_file` 调用结果，不再把 terminal 或其他工具偶然吐出的相同文本算作读取证据；同时保留 `truncated`/`next_offset`，把缺少结果的 read call 单独列出，并只在后续、同一路径、明确带 `limit` 且请求 `offset=next_offset` 时标记 continuation。它仍不能证明语义理解；如果 continuation 自身仍然被截断，报告会单独标出，不能把“接上了下一段”误写成“已经读完”。

## 下一步

区分同一路径多次读取时的覆盖合并与重复来源，并让 continuation 关联进一步核对后续结果是否仍然截断。相对路径匹配已使用 `--base` 解析，避免把真实读取误判成未匹配。多段 continuation 现在会合并成显式链（例如 `call_a -> call_b -> call_c`），不会因找到第二段就停止检查。

本轮还补上了一个证据边界：只有后续 continuation 自身也有已存储的 tool result，才会被标记为 continuation；仅有调用记录的后续读取不能冒充已读取内容。多段 continuation 会沿着 `next_offset` 递归关联，报告完整链条和链尾是否仍被截断。同一条 assistant message 中并行发出的读取调用不会互相冒充 continuation。
