"""LangGraph multi-agent crew for WordPressGenius."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from core.llm import (
    LLMRouter,
    extract_json_object,
    fallback_blog_copy,
    fallback_page_copy,
)
from core.memory import ActivityLog, SiteMemory
from core.models import (
    AgentStatus,
    ChangeOperation,
    ExecutionResult,
    PlannedAction,
    RiskLevel,
)
from core.preview import build_change_previews, render_previews
from core.safety import SafetyLayer
from tools.web_search import run_web_search, web_search_available
from tools.wordpress_tools import WordPressTools


LOGGER = logging.getLogger(__name__)

# Cap raw web-search text fed to the model to control token cost.
_WEB_RESULT_CAP = 4000

# Re-export for tests/back-compat: the centralized JSON parser now lives in core.llm.
_parse_json_object = extract_json_object

# System prompts give each agent a clear role so the shared model can apply its
# full intelligence. Kept concise to control token cost.
PLANNER_SYSTEM = (
    "You are the planning brain of a careful WordPress management assistant. "
    "Convert the user's request into a minimal list of concrete actions using "
    "ONLY the operations provided. Extract ids, percentages, slugs, and titles "
    "from the request. Set status to 'publish' ONLY if the user explicitly asks "
    "to publish or go live; otherwise omit status (drafts are default). Do NOT "
    "write post/page body content; another agent does that. Prefer a single "
    "action. For plugin work beyond core content/WooCommerce, use plugin_read "
    "(read-only) or plugin_action (changes, approval-gated) with payload "
    "{plugin, action, ...}; supported plugins include woocommerce, elementor, "
    "seo, security, forms, maintenance, and acf. When unsure what is installed, "
    "use plugin_inventory first. If the request is just a question or unclear, "
    "use read_only."
)
RESEARCHER_SYSTEM = (
    "You are a WordPress research analyst working with a store that may run "
    "WooCommerce, Stripe, SEO plugins (Yoast/Rank Math), Elementor, Contact Form 7, "
    "Jetpack, Akismet, and more. Use the site's detected plugin capabilities and "
    "REST namespaces to ground your thinking about what is possible. Given the "
    "user's request and the site context gathered so far, decide whether a live "
    "web search is needed and write a short insight. Request a web search ONLY for "
    "external, real-world facts not in site memory or the site itself: product "
    "details, specifications, current pricing, reviews, comparisons, plugin "
    "documentation, or recent events. Do NOT search for site-internal tasks "
    "(editing or deleting existing pages, settings, plugins, products). "
    'Respond as JSON: {"search_query": "concise search terms, or an empty string '
    'if no external research is needed", "insight": "1-2 crisp, practical '
    'sentences on what matters before acting"}.'
)
WEB_SUMMARY_SYSTEM = (
    "You distill raw web search results into a clean, factual research brief for "
    "drafting WordPress content. In 2-3 sentences, capture concrete specifications, "
    "pricing, and notable points. Do not fabricate; if results are thin or "
    "conflicting, say so plainly. No link dumps and no markdown headers."
)
CONTENT_WRITER_SYSTEM = (
    "You are a senior small-business WordPress copywriter for a fitness and "
    "nutrition brand. Write warm, clear, conversion-focused copy. Return "
    "Gutenberg-compatible HTML blocks only. Include a practical call to action, "
    "note where real photos should be added, and avoid unsupported health claims."
)
EXECUTOR_SYSTEM = (
    "You are a pre-flight safety reviewer that runs right before WordPress changes "
    "are applied. Compare the planned actions to the user's request and judge "
    "whether they align. You CANNOT change permissions or skip approvals; you only "
    'advise. Respond as JSON: {"aligned": true|false, "concern": "short note or empty"}.'
)
REVIEWER_SYSTEM = (
    "You are a meticulous QA reviewer. Given the executed actions and their API "
    "results, briefly and critically assess the outcome in 1-2 sentences: what "
    "succeeded, what needs attention, and any verification the owner should do. "
    "Some plugin actions degrade gracefully (a result may say a capability is not "
    "exposed via REST and give dashboard steps instead); treat that as a valid, "
    "safe outcome rather than a failure. Do not invent results beyond the data."
)
COMMUNICATOR_SYSTEM = (
    "You are a friendly, sharp WordPress assistant for a small business. Explain "
    "to the owner, in clear plain English (markdown, concise), what you understood, "
    "what you planned, and what happened. If awaiting_approval is true, clearly "
    "state you have NOT changed anything yet and explain exactly what you will do "
    "once they approve, and why approval is needed. If actions executed, summarize "
    "outcomes and give a practical next step. Never invent results not in the data."
)

# Operations the LLM planner may choose, with guidance and expected payloads.
OPERATION_GUIDE: dict[ChangeOperation, str] = {
    ChangeOperation.CREATE_POST: "Create a new blog post. payload: {title, status('draft'|'publish')}",
    ChangeOperation.UPDATE_POST: "Edit an existing post. payload: {id:int, status?}",
    ChangeOperation.DELETE_POST: "Delete a post. payload: {id:int}",
    ChangeOperation.CREATE_PAGE: "Create a new page. payload: {title, status('draft'|'publish')}",
    ChangeOperation.UPDATE_PAGE: "Edit an existing page. payload: {id:int, status?}",
    ChangeOperation.DELETE_PAGE: "Delete a page. payload: {id:int}",
    ChangeOperation.UPLOAD_MEDIA: "Upload an attached file. payload: {file_path}",
    ChangeOperation.UPDATE_SETTINGS: "Change site settings. payload: {settings:{}}",
    ChangeOperation.UPDATE_THEME: "Change the active theme. payload: {stylesheet, fields:{status}}",
    ChangeOperation.UPDATE_PLUGIN: "Activate/deactivate/update a plugin. payload: {plugin_slug, fields:{status}}",
    ChangeOperation.TROUBLESHOOT_FORM: "Inspect contact/forms for issues. payload: {search}",
    ChangeOperation.SEO_OPTIMIZE: "Improve SEO for a post/page. payload: {id:int, title?}",
    ChangeOperation.BULK_UPDATE_PRODUCTS: "Adjust WooCommerce prices by percent. payload: {percent:float}",
    ChangeOperation.PLUGIN_TROUBLESHOOT: "Diagnose a plugin. payload: {plugin_slug}",
    ChangeOperation.ANALYTICS_SUMMARY: "Summarize analytics/traffic options. payload: {}",
    ChangeOperation.UNDO_LAST_CHANGE: "Undo the latest supported change. payload: {}",
    ChangeOperation.WOOCOMMERCE_READ: (
        "Read WooCommerce data (no changes). payload: {action: "
        "'overview'|'list_products'|'get_product'|'list_orders'|'get_order'|'list_categories'|"
        "'list_tags'|'list_variations'|'list_customers'|'low_stock', id?:int, search?, status?, per_page?}"
    ),
    ChangeOperation.WOOCOMMERCE_WRITE: (
        "Change WooCommerce data (approval required). payload: {action: "
        "'create_product'|'create_products'|'update_product'|'delete_product'|'update_stock'|"
        "'update_order_status'|'bulk_price'|'bulk_stock'|'bulk_status', id?:int, product?:{}, "
        "products?:[], fields?:{}, percent?:float, stock_quantity?:int, status?}"
    ),
    ChangeOperation.STRIPE_READ: (
        "Read Stripe gateway info (no changes). payload: {action: 'status'|'transactions'|'settings', per_page?}"
    ),
    ChangeOperation.STRIPE_REFUND: (
        "Refund a WooCommerce/Stripe order (approval required). payload: {order_id:int, amount?, reason?}"
    ),
    ChangeOperation.PLUGIN_INVENTORY: (
        "Detect installed plugins, recognized capabilities, and REST namespaces. payload: {}"
    ),
    ChangeOperation.SEO_PLUGIN_BULK: (
        "Bulk-optimize SEO metadata (approval required). payload: {targets: 'posts'|'pages'|'products', "
        "ids?:[int], search?, max?:int}"
    ),
    ChangeOperation.PLUGIN_READ: (
        "Read-only plugin inspection via the plugin framework. payload: {plugin, action, ...}. "
        "Plugins/actions: woocommerce(sales_report,list_products,list_orders,...), "
        "elementor(list_templates,get_template,summary), seo(audit,schema_summary), "
        "security(security_summary), forms(list_forms,list_entries,troubleshoot), "
        "maintenance(backup_status,cache_status,optimization_summary,restore_summary), "
        "acf(field_groups,summary). Use plugin_inventory first if unsure what is installed."
    ),
    ChangeOperation.PLUGIN_ACTION: (
        "Plugin change/trigger (approval required) via the framework. payload: {plugin, action, ...}. "
        "Actions: elementor(duplicate_template id), seo(bulk_optimize targets/ids/schema_type), "
        "security(start_scan), maintenance(trigger_backup,clear_cache), "
        "woocommerce(create_product,update_product,delete_product,update_stock,update_order_status,refund,...)."
    ),
    ChangeOperation.READ_ONLY: "Research only; no changes. payload: {search}",
}


class AgentState(TypedDict, total=False):
    user_request: str
    approved: bool
    site_context: dict[str, Any]
    plan: list[PlannedAction]
    plan_reasoning: str
    research_notes: list[str]
    execution_results: list[ExecutionResult]
    final_response: str
    requires_confirmation: bool
    confirmation_summary: str
    statuses: list[AgentStatus]
    backup_path: str | None
    preflight_note: str
    review_note: str
    web_research: dict[str, Any]
    memory_context: dict[str, Any]
    run_id: str
    app_user: str
    app_role: str
    client_ip: str


def build_wordpress_crew(
    *,
    tools: WordPressTools | None,
    safety_layer: SafetyLayer,
    activity_log: ActivityLog,
    site_memory: SiteMemory,
    router: LLMRouter,
    agentic: bool = True,
    web_search_enabled: bool = True,
    web_search_max_results: int = 6,
):
    """Build and compile the LangGraph agent workflow.

    Every node shares the single :class:`LLMRouter` (one API key) and routes to
    the right model tier and temperature for its role. The Researcher may also
    perform a read-only DuckDuckGo web search for external facts when enabled.
    """

    use_llm = agentic and router.enabled
    web_research_on = web_search_enabled and web_search_available()

    def planner(state: AgentState) -> AgentState:
        request = state["user_request"]
        memory_context = state.get("memory_context", {})

        actions: list[PlannedAction] = []
        reasoning = ""
        plan_source = "rules"
        if use_llm:
            llm_result = _llm_plan_actions(request, router, memory_context)
            if llm_result:
                actions, reasoning = llm_result
                plan_source = "ai"
        if not actions:
            actions = _plan_actions(request)

        safety = safety_layer.evaluate(actions, app_role=state.get("app_role", "admin"))
        activity_log.append(
            "plan_created",
            f"Planned {len(actions)} action(s).",
            {
                "plan_source": plan_source,
                "reasoning": reasoning,
                "actions": [action.model_dump(mode="json") for action in actions],
                "safety": {
                    "requires_confirmation": safety.requires_confirmation,
                    "requires_backup": safety.requires_backup,
                    "highest_risk": safety.highest_risk.value,
                    "reasons": safety.reasons,
                },
                "memory_context_used": bool(memory_context),
            },
        )
        detail = f"Created {len(actions)} step plan."
        if plan_source == "ai":
            detail = f"AI plan ({len(actions)} step): " + (reasoning[:160] or "interpreted your request.")
        return {
            **state,
            "plan": actions,
            "plan_reasoning": reasoning,
            "requires_confirmation": safety.requires_confirmation,
            "confirmation_summary": _confirmation_summary(actions, safety.reasons),
            "statuses": state.get("statuses", [])
            + [
                AgentStatus(
                    agent="Planner",
                    status="complete",
                    detail=detail,
                )
            ],
        }

    def researcher(state: AgentState) -> AgentState:
        notes: list[str] = []
        context: dict[str, Any] = {}
        memory_context = state.get("memory_context", {})
        if memory_context:
            conversations = memory_context.get("conversations", [])[-3:]
            site_history = memory_context.get("site_history", [])[-5:]
            relevant = memory_context.get("relevant", [])
            if conversations:
                notes.append(f"Remembered {len(conversations)} recent conversation(s).")
            if site_history:
                notes.append(f"Remembered {len(site_history)} recent site event(s).")
            if relevant:
                context["relevant_memory"] = relevant
                top = relevant[0].get("text", "")
                notes.append(
                    f"Recalled {len(relevant)} relevant past memory item(s)"
                    + (f"; most relevant: {top}" if top else ".")
                )
            context["memory"] = memory_context

        if tools:
            try:
                context.update(tools.connection_summary())
                notes.append(
                    f"Connected to {context.get('site_name', 'the WordPress site')}."
                )
            except Exception as exc:  # noqa: BLE001 - connection state is reported.
                notes.append(f"Could not validate WordPress connection: {exc}")
        else:
            notes.append("WordPress credentials are not configured yet.")

        search_term = _extract_search_term(state["user_request"])
        if tools and search_term:
            try:
                read_action = PlannedAction(
                    operation=ChangeOperation.READ_ONLY,
                    title="Research matching site content",
                    description=f"Look for existing WordPress content matching {search_term}.",
                    payload={"search": search_term},
                )
                result = tools.execute(read_action)
                if result.success:
                    context["matching_content"] = result.data
                    notes.append(f"Searched existing content for '{search_term}'.")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Site research was limited: {exc}")

        detail = "Gathered site context and relevant content references."
        web_research: dict[str, Any] = {}
        if use_llm:
            request = state.get("user_request", "")
            decision = router.complete_json(
                "researcher",
                RESEARCHER_SYSTEM,
                _research_brief(request, context, state.get("plan", [])),
            )
            insight = ""
            query = ""
            if isinstance(decision, dict):
                insight = str(decision.get("insight", "")).strip()
                query = str(decision.get("search_query", "")).strip()

            # Live web search is Researcher-only, read-only, and runs only when the
            # model asks for it with a real query. Results are summarized cleanly
            # before they ever reach content creation.
            if web_research_on and query:
                raw = run_web_search(query, max_results=web_search_max_results)
                if raw:
                    web_research = {"query": query, "results": raw[:_WEB_RESULT_CAP]}
                    notes.append(f"Searched the web for '{query}'.")
                    summary = router.complete(
                        "researcher",
                        WEB_SUMMARY_SYSTEM,
                        _web_summary_brief(request, query, raw),
                    )
                    if summary:
                        summary = summary.strip()
                        web_research["summary"] = summary
                        insight = summary
                    context["web_research"] = web_research
                    detail = "Researched live web sources and analyzed the request."

            if insight:
                context["ai_insight"] = insight
                notes.append(f"Insight: {insight}")
                if not web_research:
                    detail = "Analyzed the request against site context."

        return {
            **state,
            "site_context": context,
            "research_notes": notes,
            "web_research": web_research,
            "statuses": state.get("statuses", [])
            + [
                AgentStatus(
                    agent="Researcher",
                    status="complete",
                    detail=detail,
                )
            ],
        }

    def content_writer(state: AgentState) -> AgentState:
        request = state["user_request"]
        research = (state.get("site_context") or {}).get("web_research", {}).get("summary", "")
        updated_plan: list[PlannedAction] = []
        for action in state.get("plan", []):
            payload = dict(action.payload)
            if action.operation in {ChangeOperation.CREATE_POST, ChangeOperation.CREATE_PAGE}:
                payload.setdefault("title", _title_from_request(request, action.operation))
                payload.setdefault("content", _draft_content(request, action.operation, router, research))
                payload.setdefault("status", "draft")
            elif action.operation in {ChangeOperation.UPDATE_POST, ChangeOperation.UPDATE_PAGE}:
                payload.setdefault("content", _draft_content(request, action.operation, router, research))
            updated_plan.append(action.model_copy(update={"payload": payload}))

        return {
            **state,
            "plan": updated_plan,
            "statuses": state.get("statuses", [])
            + [
                AgentStatus(
                    agent="Content Writer",
                    status="complete",
                    detail="Prepared draft content and SEO-friendly copy where needed.",
                )
            ],
        }

    def executor(state: AgentState) -> AgentState:
        if not tools:
            return {
                **state,
                "execution_results": [
                    ExecutionResult(
                        action_title="Connect WordPress",
                        operation=ChangeOperation.READ_ONLY,
                        success=False,
                        message="Enter your WordPress site URL, username, and Application Password in the sidebar.",
                    )
                ],
                "statuses": state.get("statuses", [])
                + [
                    AgentStatus(
                        agent="WordPress Executor",
                        status="waiting",
                        detail="Waiting for WordPress credentials.",
                    )
                ],
            }

        actions = state.get("plan", [])
        user_roles = _roles_from_context(state.get("site_context", {}))
        user_capabilities = _capabilities_from_context(state.get("site_context", {}))
        current_safety = safety_layer.evaluate(
            actions,
            user_roles=user_roles,
            app_role=state.get("app_role", "admin"),
            user_capabilities=user_capabilities,
        )
        if not current_safety.allowed:
            blockers = current_safety.blockers
            activity_log.append(
                "permission_blocked",
                "Blocked plan because the connected WordPress user lacks required roles.",
                {"blockers": blockers, "roles": user_roles},
            )
            return {
                **state,
                "requires_confirmation": False,
                "execution_results": [
                    ExecutionResult(
                        action_title="Permission check",
                        operation=ChangeOperation.READ_ONLY,
                        success=False,
                        message=" ".join(blockers),
                    )
                ],
                "statuses": state.get("statuses", [])
                + [
                    AgentStatus(
                        agent="WordPress Executor",
                        status="blocked",
                        detail="Connected WordPress user role cannot perform the requested action.",
                    )
                ],
            }

        if current_safety.requires_confirmation and not state.get("approved"):
            previews = build_change_previews(tools, actions)
            summary = _confirmation_summary(actions, current_safety.reasons)
            preview_text = render_previews(previews)
            if preview_text:
                summary = f"{summary}\n\n{preview_text}"
            if current_safety.requires_backup:
                summary += (
                    "\n\nSafety tip: WordPressGenius will snapshot the items it changes, but for "
                    "high-risk edits run a full-site backup first (e.g. UpdraftPlus > Backup Now)."
                )
            activity_log.append(
                "approval_required",
                "Waiting for explicit user approval before WordPress changes.",
                {"reasons": current_safety.reasons, "previews": len(previews)},
            )
            return {
                **state,
                "requires_confirmation": True,
                "confirmation_summary": summary,
                "execution_results": [],
                "statuses": state.get("statuses", [])
                + [
                    AgentStatus(
                        agent="WordPress Executor",
                        status="waiting",
                        detail="Waiting for explicit approval before publishing, updating, or deleting anything.",
                    )
                ],
            }

        # Intelligent pre-flight: advisory only. The model double-checks that the
        # plan matches the request before we touch WordPress. It can flag a concern
        # but never bypass safety/permissions (those are already enforced above).
        preflight_note = ""
        changing_actions = [a for a in actions if a.operation != ChangeOperation.READ_ONLY]
        if use_llm and changing_actions:
            verdict = router.complete_json(
                "executor",
                EXECUTOR_SYSTEM,
                _preflight_brief(state.get("user_request", ""), actions),
            )
            if isinstance(verdict, dict) and verdict.get("aligned") is False:
                concern = str(verdict.get("concern", "")).strip() or "The plan may not match your request."
                preflight_note = concern
                activity_log.append(
                    "preflight_concern",
                    "Pre-flight review flagged a possible mismatch.",
                    {"concern": concern},
                )

        backup_path = None
        if actions:
            safety = current_safety
            if safety.requires_backup:
                backup = tools.create_backup(actions)
                backup_path = str(backup) if backup else None
                if backup_path:
                    activity_log.append(
                        "backup_created",
                        "Created backup before major changes.",
                        {"backup_path": backup_path},
                    )

        results = tools.execute_many(actions)
        activity_log.append(
            "actions_executed",
            "Executed WordPress plan.",
            {"results": [result.model_dump(mode="json") for result in results]},
        )
        executor_detail = f"Executed {len(results)} action(s)."
        if preflight_note:
            executor_detail += f" Pre-flight note: {preflight_note}"
        return {
            **state,
            "execution_results": results,
            "backup_path": backup_path,
            "preflight_note": preflight_note,
            "statuses": state.get("statuses", [])
            + [
                AgentStatus(
                    agent="WordPress Executor",
                    status="complete",
                    detail=executor_detail,
                )
            ],
        }

    def reviewer(state: AgentState) -> AgentState:
        results = state.get("execution_results", [])
        if not results:
            detail = "No live changes were made; the plan is either waiting for approval or credentials."
        elif all(result.success for result in results):
            detail = "All completed actions returned successful WordPress API responses."
        else:
            failures = [result.message for result in results if not result.success]
            detail = "Some actions need attention: " + "; ".join(failures)

        review_note = ""
        if use_llm and results:
            critique = router.complete(
                "reviewer",
                REVIEWER_SYSTEM,
                _review_brief(state.get("user_request", ""), results),
            )
            if critique:
                review_note = critique.strip()
                detail = review_note

        return {
            **state,
            "review_note": review_note,
            "statuses": state.get("statuses", [])
            + [AgentStatus(agent="Reviewer", status="complete", detail=detail)],
        }

    def communicator(state: AgentState) -> AgentState:
        response = ""
        if use_llm:
            response = _llm_compose_response(state, router) or ""
        if not response:
            response = _format_response(state)
        activity_log.append("response_ready", "Prepared user-facing response.")
        site_memory.remember_conversation(
            state.get("user_request", ""),
            response,
            {
                "requires_confirmation": bool(state.get("requires_confirmation")),
                "result_count": len(state.get("execution_results", [])),
            },
        )
        if state.get("execution_results"):
            site_memory.remember_site_event(
                "execution",
                response,
                {
                    "backup_path": state.get("backup_path"),
                    "operations": [
                        result.operation.value for result in state.get("execution_results", [])
                    ],
                },
            )
        return {
            **state,
            "final_response": response,
            "statuses": state.get("statuses", [])
            + [
                AgentStatus(
                    agent="Communicator",
                    status="complete",
                    detail="Summarized progress and next steps.",
                )
            ],
        }

    graph = StateGraph(AgentState)
    graph.add_node("planner", planner)
    graph.add_node("researcher", researcher)
    graph.add_node("content_writer", content_writer)
    graph.add_node("executor", executor)
    graph.add_node("reviewer", reviewer)
    graph.add_node("communicator", communicator)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("researcher", "content_writer")
    graph.add_edge("content_writer", "executor")
    graph.add_edge("executor", "reviewer")
    graph.add_edge("reviewer", "communicator")
    graph.add_edge("communicator", END)
    return graph.compile()


def _llm_plan_actions(
    request: str, router: LLMRouter, memory_context: dict[str, Any]
) -> tuple[list[PlannedAction], str] | None:
    """Use the model to turn a request into validated, structured actions.

    Returns (actions, reasoning) or None to fall back to the rule-based planner.
    The model only selects operations and parameters; the SafetyLayer remains the
    authority on confirmation, backups, and permissions.
    """

    operations = "\n".join(f"- {op.value}: {guide}" for op, guide in OPERATION_GUIDE.items())
    system_prompt = (
        f"{PLANNER_SYSTEM}\n\nOperations:\n{operations}\n\n"
        'Respond ONLY with JSON: {"reasoning": "one sentence", "actions": '
        '[{"operation": "<op>", "title": "short title", "description": "what/why", '
        '"payload": {}, "risk": "low|medium|high"}]}'
    )
    recent = memory_context.get("conversations", []) if isinstance(memory_context, dict) else []
    memory_hint = ""
    if recent:
        last = recent[-1]
        memory_hint = f"\nRecent context: {str(last.get('request', ''))[:200]}"
    data = router.complete_json(
        "planner",
        system_prompt,
        f"User request:\n{request}{memory_hint}",
    )
    if not data:
        return None

    actions = _actions_from_payload(data.get("actions", []))
    if not actions:
        return None
    reasoning = str(data.get("reasoning", "")).strip()
    return actions, reasoning


def _actions_from_payload(raw_actions: Any) -> list[PlannedAction]:
    if not isinstance(raw_actions, list):
        return []
    valid_ops = {op.value: op for op in ChangeOperation}
    risk_map = {"low": RiskLevel.LOW, "medium": RiskLevel.MEDIUM, "high": RiskLevel.HIGH}
    actions: list[PlannedAction] = []
    for item in raw_actions[:4]:
        if not isinstance(item, dict):
            continue
        operation = valid_ops.get(str(item.get("operation", "")).strip().lower())
        if operation is None:
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        risk = risk_map.get(str(item.get("risk", "")).strip().lower(), RiskLevel.LOW)
        title = str(item.get("title") or operation.value.replace("_", " ").title())[:80]
        description = str(item.get("description") or title)[:300]
        try:
            actions.append(
                PlannedAction(
                    operation=operation,
                    title=title,
                    description=description,
                    payload=payload,
                    risk=risk,
                )
            )
        except Exception:  # noqa: BLE001 - skip malformed actions
            LOGGER.debug("Skipped malformed LLM action: %s", item)
    return actions


def _research_brief(request: str, context: dict[str, Any], plan: list[PlannedAction]) -> str:
    site_name = context.get("site_name", "unknown")
    matching = context.get("matching_content")
    match_note = "yes" if matching else "none found/searched"
    plan_ops = ", ".join(a.operation.value for a in plan) or "not planned yet"
    relevant = context.get("relevant_memory", [])
    recall = ""
    if relevant:
        items = "; ".join(item.get("text", "") for item in relevant[:3])
        recall = f"\nRelevant long-term memory: {items}"
    return (
        f"User request: {request}\n"
        f"Connected site: {site_name}\n"
        f"Matching existing content: {match_note}\n"
        f"Planned operations: {plan_ops}"
        f"{recall}"
    )


def _web_summary_brief(request: str, query: str, raw_results: str) -> str:
    return (
        f"User request:\n{request}\n\n"
        f"Web search query: {query}\n\n"
        f"Raw search results:\n{raw_results[:_WEB_RESULT_CAP]}"
    )


def _preflight_brief(request: str, actions: list[PlannedAction]) -> str:
    lines = "\n".join(
        f"- {a.operation.value}: {a.title} (payload={a.payload})" for a in actions
    )
    return f"User request:\n{request}\n\nPlanned actions about to run:\n{lines}"


def _review_brief(request: str, results: list[ExecutionResult]) -> str:
    lines = "\n".join(
        f"- {'OK' if r.success else 'FAILED'}: {r.action_title} - {r.message}"
        for r in results
    )
    return f"User request:\n{request}\n\nExecuted results:\n{lines}"


def _llm_compose_response(state: AgentState, router: LLMRouter) -> str | None:
    """Produce an intelligent, plain-English summary of the plan and results."""

    plan = state.get("plan", [])
    plan_lines = [
        f"- {a.title} ({a.operation.value}; payload={a.payload})" for a in plan
    ]
    results = state.get("execution_results", [])
    result_lines = [
        f"- {'OK' if r.success else 'FAILED'}: {r.action_title} - {r.message}"
        + (f" link={r.data.get('link')}" if r.data and r.data.get("link") else "")
        for r in results
    ]
    pending = bool(state.get("requires_confirmation")) and not state.get("approved")
    context = {
        "request": state.get("user_request", ""),
        "reasoning": state.get("plan_reasoning", ""),
        "plan": plan_lines,
        "results": result_lines or ["No changes were executed."],
        "research_notes": state.get("research_notes", []),
        "web_research": (state.get("web_research") or {}).get("summary", ""),
        "review_note": state.get("review_note", ""),
        "preflight_note": state.get("preflight_note", ""),
        "backup_path": state.get("backup_path"),
        "awaiting_approval": pending,
    }
    return router.complete(
        "communicator",
        COMMUNICATOR_SYSTEM,
        json.dumps(context, default=str),
    )


def _plan_actions(request: str) -> list[PlannedAction]:
    text = request.lower()
    actions: list[PlannedAction] = []

    publish_requested = _requested_publish(text)

    if "undo" in text and "last" in text:
        actions.append(
            PlannedAction(
                operation=ChangeOperation.UNDO_LAST_CHANGE,
                title="Undo last supported change",
                description="Restore the latest supported backup snapshot after confirmation.",
                payload={},
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "refund" in text:
        actions.append(
            PlannedAction(
                operation=ChangeOperation.STRIPE_REFUND,
                title="Refund order via Stripe",
                description="Refund a WooCommerce order through the Stripe gateway after approval.",
                payload=_order_id_payload(request),
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "stripe" in text:
        if any(token in text for token in ["transaction", "payment", "charge", "recent", "sales"]):
            stripe_action = "transactions"
        elif any(token in text for token in ["setting", "config", "key", "connection"]):
            stripe_action = "settings"
        else:
            stripe_action = "status"
        actions.append(
            PlannedAction(
                operation=ChangeOperation.STRIPE_READ,
                title="Read Stripe gateway",
                description="Summarize Stripe gateway status, settings, or recent transactions (read-only).",
                payload={"action": stripe_action},
                risk=RiskLevel.LOW,
            )
        )
    elif any(
        token in text
        for token in [
            "installed plugins",
            "what plugins",
            "which plugins",
            "list plugins",
            "plugin inventory",
            "detect plugins",
            "plugin capabilities",
            "rest endpoints",
            "rest namespaces",
        ]
    ):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.PLUGIN_INVENTORY,
                title="Inventory installed plugins",
                description="Detect installed plugins, recognized capabilities, and available REST namespaces.",
                payload={},
                risk=RiskLevel.LOW,
            )
        )
    elif "seo" in text and any(token in text for token in ["all", "bulk", "every", "products", "posts", "pages"]):
        targets = "products" if "product" in text else ("pages" if "page" in text else "posts")
        actions.append(
            PlannedAction(
                operation=ChangeOperation.SEO_PLUGIN_BULK,
                title="Bulk optimize SEO metadata",
                description="Optimize SEO titles, slugs, and meta descriptions across many items after approval.",
                payload={"targets": targets, "search": _extract_search_term(request)},
                risk=RiskLevel.MEDIUM,
                requires_confirmation=True,
            )
        )
    elif (plugin_action := _detect_plugin_request(request, text)) is not None:
        actions.append(plugin_action)
    elif _is_woocommerce_write(text):
        actions.append(_woocommerce_write_action(request, text))
    elif _is_woocommerce_read(text):
        actions.append(_woocommerce_read_action(request, text))
    elif any(token in text for token in ["analytics", "traffic", "visitors", "page views", "site stats"]):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.ANALYTICS_SUMMARY,
                title="Summarize analytics connection",
                description="Check available analytics plugins and summarize what can be reported.",
                payload={},
                risk=RiskLevel.LOW,
            )
        )
    elif "plugin" in text and any(token in text for token in ["troubleshoot", "diagnose", "broken", "why", "issue"]):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.PLUGIN_TROUBLESHOOT,
                title="Troubleshoot plugin",
                description="Review plugin status, likely updates, and practical troubleshooting steps.",
                payload={"plugin_slug": _extract_slug(request)},
                risk=RiskLevel.LOW,
            )
        )
    elif any(token in text for token in ["bulk", "all product", "all products", "prices", "price"]):
        percent = _extract_percent(request)
        actions.append(
            PlannedAction(
                operation=ChangeOperation.BULK_UPDATE_PRODUCTS,
                title="Bulk update product prices",
                description="Update WooCommerce product regular prices by the requested percentage.",
                payload={"percent": percent},
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "attached image file:" in text or "attached media file:" in text:
        actions.append(
            PlannedAction(
                operation=ChangeOperation.UPLOAD_MEDIA,
                title="Upload attached media",
                description="Upload the image attached in chat to the WordPress media library.",
                payload={"file_path": _extract_attached_file(request)},
                risk=RiskLevel.LOW,
            )
        )
    elif any(token in text for token in ["delete", "remove", "trash"]):
        target = ChangeOperation.DELETE_PAGE if "page" in text else ChangeOperation.DELETE_POST
        actions.append(
            PlannedAction(
                operation=target,
                title="Delete WordPress content",
                description="Delete the specified WordPress content after confirmation.",
                payload=_extract_id_payload(request),
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "plugin" in text and any(token in text for token in ["update", "activate", "deactivate"]):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.UPDATE_PLUGIN,
                title="Update plugin",
                description="Modify plugin status/settings via the WordPress REST API.",
                payload=_plugin_payload(request),
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "theme" in text and any(token in text for token in ["update", "switch", "activate"]):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.UPDATE_THEME,
                title="Update theme",
                description="Modify the active theme via the WordPress REST API.",
                payload=_theme_payload(request),
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "setting" in text or "settings" in text:
        actions.append(
            PlannedAction(
                operation=ChangeOperation.UPDATE_SETTINGS,
                title="Update WordPress settings",
                description="Update site settings after confirmation.",
                payload={"settings": {}},
                risk=RiskLevel.HIGH,
                requires_confirmation=True,
            )
        )
    elif "form" in text and any(token in text for token in ["fix", "broken", "troubleshoot", "repair"]):
        actions.append(
            PlannedAction(
                operation=ChangeOperation.TROUBLESHOOT_FORM,
                title="Troubleshoot form",
                description="Inspect likely form pages and plugins for common issues.",
                payload={"search": "contact"},
                risk=RiskLevel.MEDIUM,
                requires_confirmation=True,
            )
        )
    elif any(token in text for token in ["seo", "optimize", "meta description", "keywords"]):
        payload = _extract_id_payload(request)
        payload.update({"title": _title_from_request(request, ChangeOperation.SEO_OPTIMIZE)})
        actions.append(
            PlannedAction(
                operation=ChangeOperation.SEO_OPTIMIZE,
                title="Optimize SEO metadata",
                description="Improve title, slug, excerpt, and keyword guidance.",
                payload=payload,
                risk=RiskLevel.MEDIUM,
                requires_confirmation=True,
            )
        )
    elif any(token in text for token in ["blog", "article", "post"]):
        operation = (
            ChangeOperation.UPDATE_POST
            if any(token in text for token in ["update", "edit", "change"])
            else ChangeOperation.CREATE_POST
        )
        payload = _extract_id_payload(request) if operation == ChangeOperation.UPDATE_POST else {}
        if publish_requested:
            payload["status"] = "publish"
        risk = RiskLevel.MEDIUM if operation == ChangeOperation.UPDATE_POST or publish_requested else RiskLevel.LOW
        actions.append(
            PlannedAction(
                operation=operation,
                title="Update blog post" if operation == ChangeOperation.UPDATE_POST else "Create blog post",
                description=(
                    "Update an existing WordPress post after confirmation."
                    if operation == ChangeOperation.UPDATE_POST
                    else "Create a new WordPress post. It remains a draft unless publishing is explicitly approved."
                ),
                payload=payload,
                risk=risk,
                requires_confirmation=operation == ChangeOperation.UPDATE_POST or publish_requested,
            )
        )
    elif any(token in text for token in ["page", "homepage", "home page", "landing page"]):
        operation = (
            ChangeOperation.UPDATE_PAGE
            if any(token in text for token in ["update", "edit", "change"])
            else ChangeOperation.CREATE_PAGE
        )
        payload = _extract_id_payload(request)
        if publish_requested:
            payload["status"] = "publish"
        risk = RiskLevel.MEDIUM if operation == ChangeOperation.UPDATE_PAGE or publish_requested else RiskLevel.LOW
        actions.append(
            PlannedAction(
                operation=operation,
                title="Update page" if operation == ChangeOperation.UPDATE_PAGE else "Create page",
                description=(
                    "Update an existing WordPress page after confirmation."
                    if operation == ChangeOperation.UPDATE_PAGE
                    else "Create a new WordPress page. It remains a draft unless publishing is explicitly approved."
                ),
                payload=payload,
                risk=risk,
                requires_confirmation=operation == ChangeOperation.UPDATE_PAGE or publish_requested,
            )
        )
    else:
        actions.append(
            PlannedAction(
                operation=ChangeOperation.READ_ONLY,
                title="Research request",
                description="Gather site context and recommend the safest next step.",
                payload={"search": _extract_search_term(request)},
                risk=RiskLevel.LOW,
            )
        )

    return actions


def _draft_content(
    request: str, operation: ChangeOperation, router: LLMRouter, research: str = ""
) -> str:
    kind = "blog post" if operation in {ChangeOperation.CREATE_POST, ChangeOperation.UPDATE_POST} else "WordPress page"
    prompt = f"Draft a polished {kind} for this request:\n{request}"
    if research:
        prompt += (
            "\n\nUse these verified web research findings where relevant (do not "
            f"invent beyond them):\n{research}"
        )
    generated = router.complete("content_writer", CONTENT_WRITER_SYSTEM, prompt)
    if generated:
        return generated
    if operation in {ChangeOperation.CREATE_POST, ChangeOperation.UPDATE_POST}:
        return fallback_blog_copy(request)
    return fallback_page_copy(request)


def _format_response(state: AgentState) -> str:
    lines: list[str] = []
    if state.get("requires_confirmation") and not state.get("approved"):
        lines.append("I have a safe plan ready, but I need your approval before I touch WordPress.")
        lines.append("")
        lines.append(state.get("confirmation_summary", "Please confirm the proposed changes."))
        lines.append("")
        lines.append("If anything looks off, cancel the plan and send a more specific instruction.")
        return "\n".join(lines)

    results = state.get("execution_results", [])
    if not results:
        lines.append("I did not make any WordPress changes.")
    else:
        lines.append("Here is what happened:")
        for result in results:
            prefix = "Done" if result.success else "Needs attention"
            lines.append(f"- {prefix}: {result.action_title} - {result.message}")
            link = result.data.get("link") if result.data else None
            if link:
                lines.append(f"  Link: {link}")
            if result.data and result.data.get("summary"):
                lines.append(f"  Summary: {result.data['summary']}")
            if result.data and "updated_count" in result.data:
                lines.append(
                    f"  Updated: {result.data.get('updated_count', 0)}; "
                    f"skipped: {result.data.get('skipped_count', 0)}"
                )

    backup_path = state.get("backup_path")
    if backup_path:
        lines.append(f"\nBackup created before changes: `{backup_path}`")

    notes = state.get("research_notes", [])
    if notes:
        lines.append("\nWhat I checked:")
        lines.extend(f"- {note}" for note in notes)

    if results and all(result.success for result in results):
        lines.append("\nNext step: review the draft or page in WordPress when convenient, then publish when ready.")
    return "\n".join(lines)


def _confirmation_summary(actions: list[PlannedAction], reasons: list[str]) -> str:
    lines = ["Proposed actions:"]
    for action in actions:
        status = action.payload.get("status")
        status_note = f", requested status: {status}" if status else ""
        lines.append(
            f"- {action.title}: {action.description} "
            f"(risk: {action.risk.value}{status_note})"
        )
    if reasons:
        lines.append("\nWhy I am asking first:")
        lines.extend(f"- {reason}" for reason in reasons)
    lines.append("\nApprove only if this is exactly what you want WordPressGenius to do.")
    return "\n".join(lines)


def _roles_from_context(context: dict[str, Any]) -> list[str]:
    user = context.get("user", {})
    roles = user.get("roles", []) if isinstance(user, dict) else []
    return [str(role).lower() for role in roles]


def _capabilities_from_context(context: dict[str, Any]) -> list[str]:
    user = context.get("user", {})
    caps = user.get("capabilities", []) if isinstance(user, dict) else []
    return [str(cap).lower() for cap in caps] if isinstance(caps, list) else []


def _requested_publish(text: str) -> bool:
    return any(token in text for token in ["publish", "go live", "make live"])


def _extract_percent(request: str) -> float:
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", request)
    if match:
        return float(match.group(1))
    match = re.search(r"by\s+([+-]?\d+(?:\.\d+)?)", request, re.I)
    return float(match.group(1)) if match else 0.0


def _extract_attached_file(request: str) -> str:
    match = re.search(r"attached (?:image|media) file:\s*([^\n]+)", request, re.I)
    return match.group(1).strip() if match else ""


def _extract_id_payload(request: str) -> dict[str, Any]:
    match = re.search(r"\b(?:id|post|page)\s*#?\s*(\d+)\b", request, re.I)
    return {"id": int(match.group(1))} if match else {}


def _extract_search_term(request: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]+", request)
    useful = [
        word
        for word in words
        if word.lower()
        not in {
            "add",
            "create",
            "edit",
            "fix",
            "new",
            "page",
            "post",
            "the",
            "update",
            "with",
        }
    ]
    return " ".join(useful[:4])


def _title_from_request(request: str, operation: ChangeOperation) -> str:
    cleaned = re.sub(
        r"\b(add|create|new|update|edit|write|publish|draft|blog post|page|about)\b",
        "",
        request,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return "New WordPress Draft"
    title = cleaned[:70].strip()
    return title[:1].upper() + title[1:]


def _detect_plugin_request(request: str, text: str) -> PlannedAction | None:
    """Map clear plugin-specific phrasing to a PLUGIN_READ/PLUGIN_ACTION action.

    The LLM planner is the primary path; this rule-based detector keeps the
    feature usable without an API key. It deliberately covers only unambiguous
    plugin keywords so it does not hijack generic content/troubleshooting intents.
    """

    def read(plugin: str, action: str, title: str, **extra: Any) -> PlannedAction:
        return PlannedAction(
            operation=ChangeOperation.PLUGIN_READ,
            title=title,
            description="Read-only plugin inspection (no changes are made).",
            payload={"plugin": plugin, "action": action, **extra},
            risk=RiskLevel.LOW,
        )

    def write(plugin: str, action: str, title: str, **extra: Any) -> PlannedAction:
        return PlannedAction(
            operation=ChangeOperation.PLUGIN_ACTION,
            title=title,
            description="Plugin change/trigger that requires your approval.",
            payload={"plugin": plugin, "action": action, **extra},
            risk=RiskLevel.HIGH,
            requires_confirmation=True,
        )

    if "elementor" in text:
        if "duplicate" in text:
            return write("elementor", "duplicate_template", "Duplicate Elementor template", **_resource_id_payload(request))
        if any(t in text for t in ["template", "templates", "library"]):
            return read("elementor", "list_templates", "List Elementor templates")
        return read("elementor", "summary", "Summarize Elementor usage")
    if any(t in text for t in ["wordfence", "malware", "security scan"]) or (
        "security" in text and any(t in text for t in ["summary", "status", "report", "check", "scan"])
    ):
        if "scan" in text and any(v in text for v in ["run", "start", "trigger", "perform", "do a"]):
            return write("security", "start_scan", "Start security scan")
        return read("security", "security_summary", "Security summary")
    if "updraft" in text or ("backup" in text and "plugin" in text):
        if any(v in text for v in ["trigger", "run", "start", "take", "create", "make"]):
            return write("maintenance", "trigger_backup", "Trigger full-site backup")
        return read("maintenance", "backup_status", "Backup status")
    if any(t in text for t in ["cache", "litespeed", "wp rocket", "wp-rocket"]):
        if any(v in text for v in ["clear", "purge", "flush", "empty"]):
            return write("maintenance", "clear_cache", "Clear site cache")
        return read("maintenance", "cache_status", "Cache status")
    if "acf" in text or "custom field" in text:
        return read("acf", "field_groups", "List ACF field groups")
    if (
        ("form" in text and any(v in text for v in ["list", "show", "view", "all"]))
        or "submission" in text
        or "entries" in text
        or "gravity" in text
        or "wpforms" in text
    ):
        if any(t in text for t in ["submission", "entries", "entry"]):
            return read("forms", "list_entries", "List form submissions", **_resource_id_payload(request))
        return read("forms", "list_forms", "List site forms")
    if "schema" in text:
        if any(v in text for v in ["set", "add", "apply", "bulk", "all"]):
            targets = "products" if "product" in text else ("pages" if "page" in text else "posts")
            return write("seo", "bulk_optimize", "Bulk SEO + schema update", targets=targets, search=_extract_search_term(request))
        return read("seo", "schema_summary", "SEO schema summary")
    if ("rank math" in text or "rankmath" in text or "yoast" in text) and any(
        t in text for t in ["audit", "report", "check", "summary"]
    ):
        return read("seo", "audit", "SEO audit")
    if "sales report" in text or ("sales" in text and "report" in text):
        return read("woocommerce", "sales_report", "WooCommerce sales report")
    return None


def _is_woocommerce_write(text: str) -> bool:
    if "product" in text and any(v in text for v in ["create", "add", "new"]) and not any(
        n in text for n in ["post", "page", "blog"]
    ):
        return True
    if "product" in text and any(v in text for v in ["delete", "remove", "trash"]):
        return True
    if any(n in text for n in ["stock", "inventory"]) and any(
        v in text for v in ["set", "update", "change", "adjust", "restock", "reduce"]
    ):
        return True
    if "order" in text and any(
        v in text for v in ["mark", "complete", "processing", "cancel", "status", "fulfil", "ship"]
    ):
        return True
    return False


def _woocommerce_write_action(request: str, text: str) -> PlannedAction:
    if any(n in text for n in ["stock", "inventory"]):
        payload: dict[str, Any] = {"action": "update_stock", **_resource_id_payload(request)}
        quantity = _extract_first_int(request)
        if quantity is not None:
            payload["stock_quantity"] = quantity
        title, description = "Update product stock", "Set WooCommerce stock for a product after approval."
    elif "order" in text:
        payload = {"action": "update_order_status", **_resource_id_payload(request), "status": _extract_order_status(text)}
        title, description = "Update order status", "Change a WooCommerce order status after approval."
    elif any(v in text for v in ["delete", "remove", "trash"]):
        payload = {"action": "delete_product", **_resource_id_payload(request)}
        title, description = "Delete product", "Delete a WooCommerce product after approval."
    else:
        payload = {"action": "create_product", "product": {"name": _title_from_request(request, ChangeOperation.CREATE_POST)}}
        title, description = "Create product", "Create a WooCommerce product as a draft after approval."
    return PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_WRITE,
        title=title,
        description=description,
        payload=payload,
        risk=RiskLevel.HIGH,
        requires_confirmation=True,
    )


def _is_woocommerce_read(text: str) -> bool:
    woo_nouns = any(
        noun in text
        for noun in [
            "product",
            "order",
            "woocommerce",
            "customer",
            "categor",
            "variation",
            "sku",
            "inventory",
            "low stock",
        ]
    )
    if not woo_nouns:
        return False
    read_verbs = any(
        verb in text
        for verb in ["show", "list", "view", "get", "display", "how many", "what", "see", "check", "report"]
    )
    if "price" in text and not read_verbs:
        return False
    return read_verbs or any(
        noun in text for noun in ["order", "customer", "woocommerce", "variation", "categor", "inventory", "low stock"]
    )


def _woocommerce_read_action(request: str, text: str) -> PlannedAction:
    if ("low" in text and "stock" in text) or ("inventory" in text and "report" in text):
        payload: dict[str, Any] = {"action": "low_stock"}
        title = "Low stock report"
    elif "order" in text:
        ids = _resource_id_payload(request)
        if "id" in ids:
            payload, title = {"action": "get_order", **ids}, "View order"
        else:
            payload, title = {"action": "list_orders"}, "List recent orders"
    elif "customer" in text:
        payload, title = {"action": "list_customers"}, "List customers (contact details masked)"
    elif "categor" in text:
        payload, title = {"action": "list_categories"}, "List product categories"
    elif "tag" in text:
        payload, title = {"action": "list_tags"}, "List product tags"
    elif "variation" in text:
        payload, title = {"action": "list_variations", **_resource_id_payload(request)}, "List product variations"
    else:
        ids = _resource_id_payload(request)
        if "id" in ids:
            payload, title = {"action": "get_product", **ids}, "View product"
        else:
            payload, title = {"action": "list_products", "search": _extract_search_term(request)}, "List products"
    return PlannedAction(
        operation=ChangeOperation.WOOCOMMERCE_READ,
        title=title,
        description="Read WooCommerce data (no changes are made).",
        payload=payload,
        risk=RiskLevel.LOW,
    )


def _resource_id_payload(request: str) -> dict[str, Any]:
    match = re.search(r"\b(?:id|product|order|item)\s*#?\s*(\d+)\b", request, re.I)
    if not match:
        match = re.search(r"#\s*(\d+)\b", request)
    return {"id": int(match.group(1))} if match else {}


def _order_id_payload(request: str) -> dict[str, Any]:
    payload = _resource_id_payload(request)
    return {"order_id": payload["id"]} if "id" in payload else {}


def _extract_first_int(request: str) -> int | None:
    match = re.search(r"\b(\d+)\b", request)
    return int(match.group(1)) if match else None


def _extract_order_status(text: str) -> str:
    for status in ["completed", "processing", "refunded", "on-hold", "pending", "failed"]:
        if status in text:
            return status
    if "complete" in text or "fulfil" in text or "ship" in text:
        return "completed"
    if "cancel" in text:
        return "cancelled"
    return "processing"


def _plugin_payload(request: str) -> dict[str, Any]:
    status = "active"
    if "deactivate" in request.lower():
        status = "inactive"
    return {"plugin_slug": _extract_slug(request), "fields": {"status": status}}


def _theme_payload(request: str) -> dict[str, Any]:
    return {"stylesheet": _extract_slug(request), "fields": {"status": "active"}}


def _extract_slug(request: str) -> str:
    quoted = re.search(r"['\"]([^'\"]+)['\"]", request)
    if quoted:
        return quoted.group(1).strip()
    words = re.findall(r"[a-z0-9-]+", request.lower())
    ignored = {"activate", "deactivate", "plugin", "theme", "switch", "update"}
    candidates = [word for word in words if word not in ignored]
    return candidates[-1] if candidates else ""
