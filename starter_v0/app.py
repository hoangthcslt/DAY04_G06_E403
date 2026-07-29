from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import streamlit as st

from chat import now_iso, run_model_tool_loop, safe_slug, trim_history, write_transcript
from env_loader import load_lab_env
from providers import make_provider
from tools import load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version

ROOT = Path(__file__).parent
ARTIFACTS_DIR = ROOT / "artifacts"
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
TRANSCRIPTS_DIR = ROOT / "transcripts"
load_lab_env(ROOT)

st.set_page_config(page_title="Research Agent Console", page_icon="🔎", layout="wide")


# ---------------------------------------------------------------- helpers --

def tool_status(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("error"):
            return "error"
        if result.get("status"):
            return str(result["status"])
        if result.get("awaiting_user"):
            return "awaiting_user"
    return "ok"


def status_badge(status: str) -> str:
    return {"error": "🔴", "needs_confirmation": "🟡", "awaiting_user": "🟡"}.get(status, "🟢")


def render_tool_events(rounds: list[dict[str, Any]]) -> None:
    for r in rounds:
        st.markdown(f"**Round {r['round']}**")
        if r.get("assistant_text"):
            st.write(r["assistant_text"])
        if not r.get("tool_calls"):
            st.caption("(không gọi tool)")
            continue
        for call, res in zip(r["tool_calls"], r.get("tool_results", [])):
            result = res.get("result")
            status = tool_status(result)
            st.markdown(f"{status_badge(status)} `{call['name']}` — status: `{status}`")
            c1, c2 = st.columns(2)
            c1.caption("args")
            c1.json(call["args"])
            c2.caption("result / error")
            c2.json(result)


def render_run_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    st.subheader(f"📄 {path.name}")
    summary = data.get("summary", {})
    cols = st.columns(5)
    cols[0].metric("case_accuracy", summary.get("case_accuracy"))
    cols[1].metric("tool_routing_accuracy", summary.get("tool_routing_accuracy"))
    cols[2].metric("argument_accuracy", summary.get("argument_accuracy"))
    cols[3].metric("multiturn_accuracy", summary.get("multiturn_accuracy"))
    cols[4].metric("provider_error_cases", summary.get("provider_error_cases"))
    st.caption(
        f"artifact_version=`{data.get('artifact_version')}` · provider={data.get('provider')} · "
        f"model={data.get('model')} · measured={summary.get('measured_cases')}/{summary.get('total_cases')} · "
        f"suite={data.get('suite')} · eval_cases={data.get('eval_cases')}"
    )
    if summary.get("provider_error_cases", 0) or summary.get("measured_cases") != summary.get("total_cases"):
        st.warning("provider_error_cases != 0 hoặc measured_cases != total_cases — metric của run này chưa đáng tin, xem log lỗi từng case.")

    results = data.get("results", [])
    rows = [
        {
            "id": item["id"],
            "passed": item["result"]["passed"],
            "failure_type": item["result"].get("failure_type") or "",
            "observed_mismatch": item["result"].get("observed_mismatch") or "",
        }
        for item in results
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    case_ids = [item["id"] for item in results]
    if not case_ids:
        return
    picked = st.selectbox("Xem chi tiết case", case_ids, key=f"detail_{path.name}")
    item = next(i for i in results if i["id"] == picked)
    res = item["result"]
    c1, c2 = st.columns(2)
    c1.markdown("**Input**")
    c1.write(item.get("input"))
    c1.markdown("**Expect**")
    c1.json(item["expect"])
    c2.markdown("**Actual tool calls**")
    c2.json(res.get("actual_tool_calls"))
    if res.get("actual_text"):
        c2.markdown("**Actual text**")
        c2.write(res["actual_text"])
    if res.get("failures"):
        st.warning(" · ".join(res["failures"]))
    with st.expander("Tool results (raw, gồm error/status thật)"):
        st.json(item.get("tool_results", []))


# ------------------------------------------------------------------ sidebar --

with st.sidebar:
    st.header("⚙️ Cấu hình")
    provider_name = st.selectbox("Provider", ["openrouter", "openai", "anthropic", "gemini"], index=0)
    model_override = st.text_input("Model override (để trống = default)", value="")
    version_label = st.text_input("Version label", value="v3", help="Nhãn version dùng để log, vd v0/v1/v2/v3.")
    max_tool_rounds = st.slider("Max tool rounds (chat)", 1, 8, 4)
    history_window = st.slider("History window (chat, số turn)", 0, 10, 5)

    system_prompt_path = ARTIFACTS_DIR / "system_prompt.md"
    tools_path = ARTIFACTS_DIR / "tools.yaml"
    artifact_version = build_artifact_version(version_label, system_prompt_path, tools_path)

    st.divider()
    st.caption("Artifact hiện tại (artifacts/system_prompt.md + tools.yaml)")
    st.code(artifact_version.artifact_version, language=None)
    st.caption(f"prompt_hash: `{artifact_version.prompt_hash[:16]}…`")
    st.caption(f"tools_hash: `{artifact_version.tools_hash[:16]}…`")

    st.divider()
    if st.button("🔄 Reset hội thoại", use_container_width=True):
        for key in ("history", "display_turns", "transcript", "transcript_path"):
            st.session_state.pop(key, None)
        st.rerun()

    st.divider()
    st.caption("🔒 Không nhập dữ liệu nhạy cảm — UI này có thể được chia sẻ qua tunnel công khai.")


st.title("🔎 Research Agent Console")

tab_chat, tab_eval, tab_browse = st.tabs(["💬 Live Chat", "🧪 Run Eval", "📂 Runs & Transcripts"])


# --------------------------------------------------------------- chat tab --

with tab_chat:
    st.caption(
        f"provider=`{provider_name}` · model=`{model_override or '(default)'}` · "
        f"artifact_version=`{artifact_version.artifact_version}`"
    )

    st.session_state.setdefault("history", [])
    st.session_state.setdefault("display_turns", [])
    st.session_state.setdefault("transcript", None)
    st.session_state.setdefault("transcript_path", None)

    for turn in st.session_state.display_turns:
        with st.chat_message("user"):
            st.write(turn["user"])
        with st.chat_message("assistant"):
            st.write(turn.get("assistant_text") or "(không có text)")
            st.caption(f"status=`{turn.get('status')}` · artifact_version=`{turn.get('artifact_version')}`")
            rounds = turn.get("rounds") or []
            n_calls = len(turn.get("tool_events") or [])
            if rounds:
                with st.expander(f"🔧 Tool trace — {len(rounds)} round, {n_calls} tool call"):
                    render_tool_events(rounds)

    user_text = st.chat_input("Nhập yêu cầu (vd: 'Tweet mới nhất của Sam Altman là gì?')")

    if user_text:
        system_prompt_text = system_prompt_path.read_text(encoding="utf-8")
        tool_declarations = load_tool_declarations(tools_path)
        openai_tools = to_openai_tools(tool_declarations)
        provider = make_provider(provider_name)
        selected_model = model_override or getattr(provider, "default_model", None)

        messages = [
            {"role": "system", "content": system_prompt_text},
            *trim_history(st.session_state.history, history_window),
            {"role": "user", "content": user_text},
        ]

        if st.session_state.transcript is None:
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            transcript_id = "_".join([safe_slug(version_label), safe_slug(provider_name), "ui", timestamp])
            st.session_state.transcript_path = TRANSCRIPTS_DIR / f"{transcript_id}.transcript.json"
            st.session_state.transcript = {
                "transcript_id": transcript_id,
                **artifact_version_dict(artifact_version),
                "provider": provider_name,
                "model": selected_model,
                "system_prompt": str(system_prompt_path),
                "tools": str(tools_path),
                "history_window": history_window,
                "max_tool_rounds": max_tool_rounds,
                "source": "streamlit_ui",
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "turns": [],
            }

        with st.spinner("Agent đang xử lý..."):
            try:
                result = run_model_tool_loop(
                    provider=provider,
                    messages=messages,
                    tools=openai_tools,
                    model=model_override or None,
                    max_tool_rounds=max_tool_rounds,
                )
                assistant_text = result["assistant_text"]
                st.session_state.history.append({"role": "user", "content": user_text})
                st.session_state.history.append({"role": "assistant", "content": assistant_text})
                turn_record: dict[str, Any] = {
                    "turn_index": len(st.session_state.display_turns) + 1,
                    "user": user_text,
                    "artifact_version": artifact_version.artifact_version,
                    "started_at": now_iso(),
                    **result,
                }
            except Exception as exc:
                turn_record = {
                    "turn_index": len(st.session_state.display_turns) + 1,
                    "user": user_text,
                    "artifact_version": artifact_version.artifact_version,
                    "started_at": now_iso(),
                    "status": "provider_error",
                    "assistant_text": f"{type(exc).__name__}: {exc}",
                    "rounds": [],
                    "tool_events": [],
                }
            turn_record["ended_at"] = now_iso()

        st.session_state.display_turns.append(turn_record)
        st.session_state.transcript["turns"].append(turn_record)
        write_transcript(st.session_state.transcript_path, st.session_state.transcript)
        st.rerun()


# --------------------------------------------------------------- eval tab --

with tab_eval:
    st.caption("Chạy eval suite thật bằng cách gọi lại `run_eval.py` (subprocess) — cùng một logic chấm điểm với CLI, không viết lại agent loop khác.")

    suite = st.selectbox("Suite", ["group", "base", "extension", "cross"], index=0)
    default_cases = {
        "base": "data/eval_base.json",
        "group": "data/eval_group.json",
        "extension": "data/eval_research_extension.json",
        "cross": "data/eval_group.json",
    }
    eval_cases_path = st.text_input("Eval cases file (relative to starter_v0/)", value=default_cases[suite])
    run_eval_button = st.button("▶️ Chạy eval suite", type="primary")

    if run_eval_button:
        cases_full_path = ROOT / eval_cases_path
        n_cases = None
        if cases_full_path.exists():
            try:
                n_cases = len(json.loads(cases_full_path.read_text(encoding="utf-8")).get("cases", []))
            except Exception:
                n_cases = None
        if n_cases == 0:
            st.error(f"{eval_cases_path} chưa có case nào (`cases: []`). Không có gì để chạy.")
        else:
            cmd = [
                sys.executable, "run_eval.py",
                "--provider", provider_name,
                "--version", version_label,
                "--suite", suite,
                "--eval-cases", eval_cases_path,
            ]
            if model_override:
                cmd += ["--model", model_override]
            with st.spinner(f"Đang chạy: {' '.join(cmd)}"):
                proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
            st.session_state["last_eval_stdout"] = proc.stdout
            st.session_state["last_eval_stderr"] = proc.stderr
            st.session_state["last_eval_returncode"] = proc.returncode
            st.session_state["last_eval_run_path"] = None
            if proc.returncode == 0 and RUNS_DIR.exists():
                files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    st.session_state["last_eval_run_path"] = str(files[0])
            st.rerun()

    if "last_eval_returncode" in st.session_state:
        if st.session_state["last_eval_returncode"] == 0:
            st.success("Eval chạy xong.")
        else:
            st.error("Eval lỗi — xem log bên dưới.")
        with st.expander("Log (stdout/stderr)", expanded=st.session_state["last_eval_returncode"] != 0):
            st.code(st.session_state.get("last_eval_stdout", "") or "(trống)")
            st.code(st.session_state.get("last_eval_stderr", "") or "(trống)")

    run_path = st.session_state.get("last_eval_run_path")
    if run_path and Path(run_path).exists():
        render_run_file(Path(run_path))


# ------------------------------------------------------------- browse tab --

with tab_browse:
    st.caption("Duyệt lại run/transcript đã lưu trên đĩa — dùng để chiếu cùng một scenario qua nhiều version khi demo.")
    sub_runs, sub_transcripts, sub_compare = st.tabs(["Runs", "Transcripts", "So sánh version"])

    with sub_runs:
        run_files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if RUNS_DIR.exists() else []
        if not run_files:
            st.info("Chưa có run nào trong `runs/`.")
        else:
            chosen = st.selectbox("Chọn run", run_files, format_func=lambda p: p.name)
            render_run_file(chosen)

    with sub_transcripts:
        t_files = sorted(TRANSCRIPTS_DIR.glob("*.transcript.json"), key=lambda p: p.stat().st_mtime, reverse=True) if TRANSCRIPTS_DIR.exists() else []
        if not t_files:
            st.info("Chưa có transcript nào trong `transcripts/`. Chat ở tab Live Chat để tạo transcript.")
        else:
            chosen_t = st.selectbox("Chọn transcript", t_files, format_func=lambda p: p.name)
            data = json.loads(chosen_t.read_text(encoding="utf-8"))
            st.caption(
                f"artifact_version=`{data.get('artifact_version')}` · provider={data.get('provider')} · "
                f"model={data.get('model')} · source={data.get('source', 'cli')}"
            )
            for turn in data.get("turns", []):
                with st.chat_message("user"):
                    st.write(turn.get("user"))
                with st.chat_message("assistant"):
                    st.write(turn.get("assistant_text") or "(không có text)")
                    rounds = turn.get("rounds") or []
                    if rounds:
                        with st.expander(f"🔧 Tool trace — {len(rounds)} round"):
                            render_tool_events(rounds)

    with sub_compare:
        run_files = sorted(RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime) if RUNS_DIR.exists() else []
        if len(run_files) < 2:
            st.info("Cần ít nhất 2 run trong `runs/` để so sánh version.")
        else:
            picked_files = st.multiselect(
                "Chọn các run để so sánh", run_files, default=run_files, format_func=lambda p: p.name
            )
            datasets = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in picked_files]

            st.markdown("**Metric tổng theo version**")
            metric_rows = [
                {
                    "version": d.get("version"),
                    "run_file": p.name,
                    "suite": d.get("suite"),
                    "case_accuracy": d.get("summary", {}).get("case_accuracy"),
                    "tool_routing_accuracy": d.get("summary", {}).get("tool_routing_accuracy"),
                    "argument_accuracy": d.get("summary", {}).get("argument_accuracy"),
                    "multiturn_accuracy": d.get("summary", {}).get("multiturn_accuracy"),
                }
                for p, d in datasets
            ]
            st.dataframe(metric_rows, use_container_width=True, hide_index=True)

            common_ids: set[str] | None = None
            for _, d in datasets:
                ids = {item["id"] for item in d.get("results", [])}
                common_ids = ids if common_ids is None else (common_ids & ids)

            if common_ids:
                st.markdown("**Cùng một scenario qua các version**")
                case_id = st.selectbox("Chọn scenario (case id) chung", sorted(common_ids))
                table = []
                for p, d in datasets:
                    item = next(i for i in d["results"] if i["id"] == case_id)
                    table.append({
                        "version": d.get("version"),
                        "artifact_version": d.get("artifact_version"),
                        "run_file": p.name,
                        "passed": item["result"]["passed"],
                        "failure_type": item["result"].get("failure_type") or "",
                        "actual_tool_calls": json.dumps(item["result"]["actual_tool_calls"], ensure_ascii=False),
                    })
                st.dataframe(table, use_container_width=True, hide_index=True)
            else:
                st.info("Các run được chọn không có case id chung (khác suite/eval-cases).")
