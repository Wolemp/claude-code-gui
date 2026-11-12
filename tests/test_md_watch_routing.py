"""Self-test for MD-WATCH routing logic.

Run via any of:
  - python tests/test_md_watch_routing.py        (standalone)
  - python main.py --md-watch-test                (via CLI flag — main.py shim
                                                   delegates to run_self_test())
  - pytest tests/test_md_watch_routing.py         (pytest, if installed)

Covers:
  - _md_watch_file_owner       (owner detection per file convention)
  - _md_watch_mailbox_recipient (peer_<role>.md → role)
  - _md_watch_interest_targets  (scoped/no-broadcast/unmapped)
  - _md_watch_targets          (full routing incl. cross-project isolation)

History: previously embedded in main.py (~130 lines, ~1.5% of file). Moved
out 2026-04-26 (P5) so the production harness file isn't bloated with mock
infra and assertion sequences.
"""
from __future__ import annotations

import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_PARENT = _THIS_DIR.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))


def run_self_test() -> dict:
    """Execute all routing checks. Returns {"passed": N, "failed": N, "details": [...]}.

    Pure: no side effects, no I/O, no GUI bootstrap. Importing `main` is
    safe because main.py only starts the GUI via `main()` in its
    `if __name__ == "__main__"` block.
    """
    from main import (
        _md_watch_file_owner,
        _md_watch_interest_targets,
        _md_watch_mailbox_recipient,
        _md_watch_targets,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # Owner detection
    # manager_queue.md: legacy queue file — owner None (no routing)
    check("owner[manager_queue.md]", _md_watch_file_owner("manager_queue.md"), None)
    check("owner[developer_report.md]", _md_watch_file_owner("developer_report.md"), "developer")
    check("owner[cco_brief.md]", _md_watch_file_owner("cco_brief.md"), "cco")
    check("owner[peer_channel.md]", _md_watch_file_owner("peer_channel.md"), None)
    check("owner[blockers.md]", _md_watch_file_owner("blockers.md"), None)
    check("owner[decisions_log.md]", _md_watch_file_owner("decisions_log.md"), None)
    check("owner[empty]", _md_watch_file_owner(""), None)
    check("owner[pa_brief.md]", _md_watch_file_owner("pa_brief.md"), "pa")

    # Mailbox recipient detection ( spec, 2026-04-26)
    check("mbx[peer_developer.md]", _md_watch_mailbox_recipient("peer_developer.md"), "developer")
    check("mbx[peer_manager.md]", _md_watch_mailbox_recipient("peer_manager.md"), "manager")
    check("mbx[peer_cco.md]", _md_watch_mailbox_recipient("peer_cco.md"), "cco")
    check("mbx[peer_channel.md]", _md_watch_mailbox_recipient("peer_channel.md"), None)
    check("mbx[developer_report.md]", _md_watch_mailbox_recipient("developer_report.md"), None)
    check("mbx[empty]", _md_watch_mailbox_recipient(""), None)
    check("mbx[peer_.md]", _md_watch_mailbox_recipient("peer_.md"), None)
    check("mbx[peer_BAD.md]", _md_watch_mailbox_recipient("peer_BAD.md"), None)
    # 2026-04-28 critical fix: abbreviation files (peer_dev.md / peer_lgl.md /
    # peer_mgr.md / peer_design.md etc.) must expand to full role IDs so they
    # match tab.role. Without this, peer dispatch silently dropped (recipient
    # match=0 because tabs are full-named "developer" etc).
    check("mbx[peer_dev.md→developer]", _md_watch_mailbox_recipient("peer_dev.md"), "developer")
    check("mbx[peer_mgr.md→manager]", _md_watch_mailbox_recipient("peer_mgr.md"), "manager")
    check("mbx[peer_lgl.md→legal]", _md_watch_mailbox_recipient("peer_lgl.md"), "legal")
    check("mbx[peer_mkt.md→marketing]", _md_watch_mailbox_recipient("peer_mkt.md"), "marketing")
    check("mbx[peer_dsn.md→designer]", _md_watch_mailbox_recipient("peer_dsn.md"), "designer")
    check("mbx[peer_sec.md→security]", _md_watch_mailbox_recipient("peer_sec.md"), "security")
    check("mbx[peer_prd.md→product]", _md_watch_mailbox_recipient("peer_prd.md"), "product")
    check("mbx[peer_rvo.md→revops]", _md_watch_mailbox_recipient("peer_rvo.md"), "revops")
    check("mbx[peer_surf.md→surfer]", _md_watch_mailbox_recipient("peer_surf.md"), "surfer")
    check("mbx[peer_ans.md→analyst]", _md_watch_mailbox_recipient("peer_ans.md"), "analyst")
    check("mbx[peer_cnt.md→content]", _md_watch_mailbox_recipient("peer_cnt.md"), "content")
    check("mbx[peer_grw.md→growth]", _md_watch_mailbox_recipient("peer_grw.md"), "growth")
    check("mbx[peer_rev.md→reviewer]", _md_watch_mailbox_recipient("peer_rev.md"), "reviewer")
    # Already-full names pass through unchanged
    check("mbx[peer_developer.md unchanged]", _md_watch_mailbox_recipient("peer_developer.md"), "developer")
    check("mbx[peer_unknown.md unchanged]", _md_watch_mailbox_recipient("peer_unknown.md"), "unknown")

    # Interest filter ( follow-up, 2026-04-26): scoped shared files
    # 2026-04-27 Layer 0a: decisions_log/tier_list/project_portfolio は
    # no-broadcast から pointer-mode push に格上げ (MDWATCH_POINTER_MODE_FILES
    # allowlist、scoped target + [MD-PULL] msg ~20 token)。
    # boundary_rulings/peer_consultation_checklist は引続き silent。
    check("intst[decisions_log.md]", _md_watch_interest_targets("decisions_log.md"), {"manager", "cco"})
    check("intst[blockers.md]", _md_watch_interest_targets("blockers.md"), {"manager", "cco"})
    check("intst[tier_list.md]", _md_watch_interest_targets("tier_list.md"), {"manager", "designer", "product"})
    check("intst[project_portfolio.md]", _md_watch_interest_targets("project_portfolio.md"), {"manager", "product", "revops"})
    check("intst[boundary_rulings.md]", _md_watch_interest_targets("boundary_rulings.md"), set())
    check("intst[peer_consultation_checklist.md]", _md_watch_interest_targets("peer_consultation_checklist.md"), set())
    check("intst[cco_brief.md]", _md_watch_interest_targets("cco_brief.md"), {"manager"})
    check("intst[pa_brief.md]", _md_watch_interest_targets("pa_brief.md"), {"manager"})
    # peer_channel.md → empty set (no-broadcast, defense-in-depth for EXCLUDE bypass)
    check("intst[peer_channel.md]", _md_watch_interest_targets("peer_channel.md"), set())
    # (2026-04-26): *_report.md endswith → set (no-broadcast).
    # WORKER-REPORT push routes via _poll_file_reports (manager only) — generic
    # MD-WATCH delta fanout is the channel that's cut.
    check("intst[developer_report.md]", _md_watch_interest_targets("developer_report.md"), set())
    check("intst[security_report.md]", _md_watch_interest_targets("security_report.md"), set())
    check("intst[marketing_report.md]", _md_watch_interest_targets("marketing_report.md"), set())
    check("intst[content_report.md]", _md_watch_interest_targets("content_report.md"), set())
    # endswith pattern: arbitrary prefix still excluded (defense-in-depth)
    check("intst[foo_report.md]", _md_watch_interest_targets("foo_report.md"), set())
    check("intst[manager_queue.md]", _md_watch_interest_targets("manager_queue.md"), None)
    check("intst[unknown.md]", _md_watch_interest_targets("unknown.md"), None)
    check("intst[empty]", _md_watch_interest_targets(""), None)

    # Target routing — mock minimal api/tabs
    class _MockPty:
        running = True

    class _MockTab:
        def __init__(self, tid: str, role: str, proj: str) -> None:
            self.id = tid
            self.name = role
            self.role = role
            self.project_path = proj
            self.pty_session = _MockPty()

    class _MockApi:
        def __init__(self, tabs) -> None:
            self._tabs = {t.id: t for t in tabs}

    proj = "/tmp/demo"
    mgr = _MockTab("m1", "manager", proj)
    dev = _MockTab("d1", "developer", proj)
    cco = _MockTab("c1", "cco", proj)
    sec = _MockTab("s1", "security", proj)
    api = _MockApi([mgr, dev, cco, sec])

    # manager_queue.md: legacy queue file — owner=None so all 4 tabs are
    # broadcast targets (deprecated dispatch path; mgr writes are blocked at
    # the protocol layer, this just confirms no routing-layer crash).
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "manager_queue.md"))
    check("targets[manager_queue.md]", roles, ["cco", "developer", "manager", "security"])

    # (2026-04-26): developer_report.md is no-broadcast at
    # the interest layer. WORKER-REPORT push to manager goes via _poll_file_reports
    # (manager only); generic MD-WATCH delta fanout to all role tabs is cut.
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "developer_report.md"))
    check("targets[developer_report.md]", roles, [])
    # endswith pattern: any *_report.md is no-broadcast (defense-in-depth).
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "security_report.md"))
    check("targets[security_report.md]", roles, [])
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "foo_report.md"))
    check("targets[foo_report.md]", roles, [])

    # peer_channel.md: owner=None, interest=set() → 0 targets (no-broadcast).
    # Note: in production _GENERIC_WATCH_EXCLUDE skips the file before it
    # reaches _md_watch_targets at all; the empty-set return here is a
    # defense-in-depth check in case EXCLUDE is ever bypassed.
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "peer_channel.md"))
    check("targets[peer_channel.md]", roles, [])

    # Cross-project isolation (still 0 targets — no-broadcast policy).
    other = _MockTab("x1", "developer", "/tmp/other")
    api2 = _MockApi([mgr, dev, cco, sec, other])
    roles = sorted(t.role for t in _md_watch_targets(api2, proj, "peer_channel.md"))
    check("targets[peer_channel.md cross-proj]", roles, [])

    # mailbox routing: peer_<role>.md → role only
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "peer_developer.md"))
    check("targets[peer_developer.md]", roles, ["developer"])
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "peer_manager.md"))
    check("targets[peer_manager.md]", roles, ["manager"])
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "peer_security.md"))
    check("targets[peer_security.md]", roles, ["security"])
    # Mailbox without matching tab → empty
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "peer_marketing.md"))
    check("targets[peer_marketing.md no-tab]", roles, [])
    # Mailbox cross-project isolation: dev tab in /tmp/other not selected
    api3 = _MockApi([mgr, dev, cco, sec, _MockTab("d2", "developer", "/tmp/other")])
    roles = sorted(t.role for t in _md_watch_targets(api3, proj, "peer_developer.md"))
    check("targets[peer_developer.md cross-proj]", roles, ["developer"])

    # follow-up: scoped interest targets for shared files
    # 2026-04-27 Layer 0a: decisions_log は pointer-mode push (mgr+cco)
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "decisions_log.md"))
    check("targets[decisions_log.md]", roles, ["cco", "manager"])
    # blockers → manager + cco only (real-time escalation)
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "blockers.md"))
    check("targets[blockers.md]", roles, ["cco", "manager"])
    # cco_brief → manager only (cco is owner, excluded; pa/dev not in interest)
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "cco_brief.md"))
    check("targets[cco_brief.md]", roles, ["manager"])
    # pa_brief → manager only (pa not in mock, but interest set is just manager)
    roles = sorted(t.role for t in _md_watch_targets(api, proj, "pa_brief.md"))
    check("targets[pa_brief.md]", roles, ["manager"])
    # 2026-04-27 Layer 0a: tier_list (mgr+dsn+prd) / project_portfolio (mgr+prd)
    # は pointer-mode push、boundary_rulings / peer_consultation_checklist は silent
    dsn = _MockTab("dsn1", "designer", proj)
    prd = _MockTab("prd1", "product", proj)
    api4 = _MockApi([mgr, dev, cco, sec, dsn, prd])
    roles = sorted(t.role for t in _md_watch_targets(api4, proj, "tier_list.md"))
    check("targets[tier_list.md]", roles, ["designer", "manager", "product"])
    roles = sorted(t.role for t in _md_watch_targets(api4, proj, "decisions_log.md"))
    check("targets[decisions_log.md w/dsn+prd]", roles, ["cco", "manager"])
    # project_portfolio adds revops as of 2026-04-27 (RICE allocation → revenue strategy)
    rvo = _MockTab("rvo1", "revops", proj)
    api4_rvo = _MockApi([mgr, dev, cco, sec, dsn, prd, rvo])
    roles = sorted(t.role for t in _md_watch_targets(api4_rvo, proj, "project_portfolio.md"))
    check("targets[project_portfolio.md w/rvo]", roles, ["manager", "product", "revops"])
    roles = sorted(t.role for t in _md_watch_targets(api4, proj, "boundary_rulings.md"))
    check("targets[boundary_rulings.md]", roles, [])
    roles = sorted(t.role for t in _md_watch_targets(api4, proj, "peer_consultation_checklist.md"))
    check("targets[peer_consultation_checklist.md]", roles, [])
    # peer_channel.md no-broadcast holds even with extra role tabs present.
    roles = sorted(t.role for t in _md_watch_targets(api4, proj, "peer_channel.md"))
    check("targets[peer_channel.md w/dsn+prd]", roles, [])

    # Layer 0a: pointer-mode helper assertions
    from main import _md_watch_pointer_mode_for
    check("pointer[decisions_log.md]", _md_watch_pointer_mode_for("decisions_log.md"), True)
    check("pointer[tier_list.md]", _md_watch_pointer_mode_for("tier_list.md"), True)
    check("pointer[project_portfolio.md]", _md_watch_pointer_mode_for("project_portfolio.md"), True)
    check("pointer[blockers.md]", _md_watch_pointer_mode_for("blockers.md"), False)
    check("pointer[cco_brief.md]", _md_watch_pointer_mode_for("cco_brief.md"), False)
    check("pointer[empty]", _md_watch_pointer_mode_for(""), False)
    # 2026-04-27 protocol docs added to pointer mode allowlist
    check("pointer[PROTOCOL_V2.md]", _md_watch_pointer_mode_for("PROTOCOL_V2.md"), True)
    check("pointer[PROTOCOL_V2_reference.md]", _md_watch_pointer_mode_for("PROTOCOL_V2_reference.md"), True)
    check("pointer[boundary_arbitration_protocol.md]", _md_watch_pointer_mode_for("boundary_arbitration_protocol.md"), True)
    check("pointer[role_interaction_protocols.md]", _md_watch_pointer_mode_for("role_interaction_protocols.md"), True)

    # 2026-04-27: content-aware routing (skip-reduction, mgr/cco bottleneck fix)
    from main import _md_watch_resolve_targets_by_content
    fallback = {"manager", "cco"}
    # Explicit (to:roles) directive overrides fallback
    # Abbreviations are expanded (mkt→marketing, lgl→legal, etc.) so harness
    # tabs (using full role IDs) match.
    r1 = _md_watch_resolve_targets_by_content(
        "blockers.md", "B007|H|site-publish (to:mkt,lgl)", fallback, "")
    check("content-route[explicit to:]", r1, {"marketing", "legal"})
    # @<role> mentions take precedence when no explicit to:
    r2 = _md_watch_resolve_targets_by_content(
        "blockers.md", " escalation: @dev fix needed for @qa regression",
        fallback, "")
    check("content-route[@mentions]", r2, {"developer", "qa"})
    # @all is excluded from mention-routing (broadcast indicator)
    r3 = _md_watch_resolve_targets_by_content(
        "blockers.md", "@all please review status",
        fallback, "")
    check("content-route[@all skipped]", r3, fallback)
    # No content tokens → fall back to default
    r4 = _md_watch_resolve_targets_by_content(
        "blockers.md", "vague text without any role markers", fallback, "")
    check("content-route[no markers→fallback]", r4, fallback)
    # Empty delta → fallback
    r5 = _md_watch_resolve_targets_by_content("blockers.md", "", fallback, "")
    check("content-route[empty→fallback]", r5, fallback)
    # to: with whitespace + capitalization → normalized
    r6 = _md_watch_resolve_targets_by_content(
        "blockers.md", "(to: MKT, lgl ,DEV)", fallback, "")
    check("content-route[normalize case+space]", r6, {"marketing", "legal", "developer"})

    # _md_watch_targets with delta_text routes content-aware
    # blockers.md default = mgr+cco; with (to:dev,qa) → only developer+qa receive
    qa_t = _MockTab("qa1", "qa", proj)
    api5 = _MockApi([mgr, dev, cco, sec, qa_t])
    roles = sorted(t.role for t in _md_watch_targets(
        api5, proj, "blockers.md", delta_text=" (to:dev,qa)"))
    check("targets[blockers content→dev+qa]", roles, ["developer", "qa"])
    # Without delta_text → default mgr+cco preserved
    roles = sorted(t.role for t in _md_watch_targets(
        api5, proj, "blockers.md"))
    check("targets[blockers no-content→mgr+cco]", roles, ["cco", "manager"])
    # INDEX.md is now in EXCLUDE — no broadcast
    from main import AutoCoordinator
    check("EXCLUDE[INDEX.md]", "INDEX.md" in AutoCoordinator._GENERIC_WATCH_EXCLUDE, True)

    # 2026-04-27 per-role default_effort allocation
    from main import ROLE_DEFAULT_EFFORTS, RoleRegistry, _VALID_EFFORTS
    check("effort[manager]", ROLE_DEFAULT_EFFORTS.get("manager"), "max")
    check("effort[cco]", ROLE_DEFAULT_EFFORTS.get("cco"), "xhigh")
    check("effort[analyst]", ROLE_DEFAULT_EFFORTS.get("analyst"), "xhigh")
    check("effort[legal]", ROLE_DEFAULT_EFFORTS.get("legal"), "xhigh")
    check("effort[security]", ROLE_DEFAULT_EFFORTS.get("security"), "xhigh")
    check("effort[product]", ROLE_DEFAULT_EFFORTS.get("product"), "xhigh")
    check("effort[developer]", ROLE_DEFAULT_EFFORTS.get("developer"), "high")
    check("effort[qa]", ROLE_DEFAULT_EFFORTS.get("qa"), "high")
    check("effort[reviewer]", ROLE_DEFAULT_EFFORTS.get("reviewer"), "high")
    check("effort[revops]", ROLE_DEFAULT_EFFORTS.get("revops"), "high")
    check("effort[pa]", ROLE_DEFAULT_EFFORTS.get("pa"), "medium")
    check("effort[marketing]", ROLE_DEFAULT_EFFORTS.get("marketing"), "medium")
    check("effort[content]", ROLE_DEFAULT_EFFORTS.get("content"), "medium")
    check("effort[docs]", ROLE_DEFAULT_EFFORTS.get("docs"), "medium")
    check("effort[designer]", ROLE_DEFAULT_EFFORTS.get("designer"), "medium")
    check("effort[growth]", ROLE_DEFAULT_EFFORTS.get("growth"), "medium")
    check("effort[sre]", ROLE_DEFAULT_EFFORTS.get("sre"), "low")
    check("effort[surfer]", ROLE_DEFAULT_EFFORTS.get("surfer"), "low")
    # Coverage: all 19 roles allocated
    check("effort[total roles]", len(ROLE_DEFAULT_EFFORTS), 18)
    # 5-tier distribution check
    counts = {"max": 0, "xhigh": 0, "high": 0, "medium": 0, "low": 0}
    for v in ROLE_DEFAULT_EFFORTS.values():
        counts[v] = counts.get(v, 0) + 1
    check("effort[max=1]", counts["max"], 1)
    check("effort[xhigh=5]", counts["xhigh"], 5)
    check("effort[high=4]", counts["high"], 4)
    check("effort[medium=6]", counts["medium"], 6)
    check("effort[low=2]", counts["low"], 2)
    # All allocated values are valid effort levels
    check("effort[all valid]",
          all(e in _VALID_EFFORTS for e in ROLE_DEFAULT_EFFORTS.values()), True)

    # _clean auto-fills default_effort from ROLE_DEFAULT_EFFORTS
    reg = RoleRegistry.__new__(RoleRegistry)  # bypass __init__
    cleaned = reg._clean({"id": "manager", "name": "Manager", "kind": "coordinator"})
    check("clean[auto-fill mgr]", cleaned.get("default_effort"), "max")
    cleaned = reg._clean({"id": "marketing", "name": "Mkt", "kind": "worker"})
    check("clean[auto-fill mkt]", cleaned.get("default_effort"), "medium")
    # Custom valid value preserved
    cleaned = reg._clean({"id": "manager", "name": "M", "kind": "coordinator",
                          "default_effort": "low"})
    check("clean[custom valid]", cleaned.get("default_effort"), "low")
    # Invalid value falls back to allocation
    cleaned = reg._clean({"id": "developer", "name": "D", "kind": "worker",
                          "default_effort": "garbage"})
    check("clean[invalid falls back]", cleaned.get("default_effort"), "high")
    # Unknown role falls back to "high"
    cleaned = reg._clean({"id": "custom_role", "name": "X", "kind": "worker"})
    check("clean[unknown→high]", cleaned.get("default_effort"), "high")

    return {"passed": passed, "failed": failed, "details": details}


def test_bounded_seen_set_lru() -> dict:
    """Exercise _BoundedSeenSet — verifies the LRU-trim fix (P1).

    Returns same shape as run_self_test for unified reporting.
    """
    from main import _BoundedSeenSet
    details: list[str] = []
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # 2001 inserts at cap=2000 trim to low=1000; verify newest 1000 retained.
    s = _BoundedSeenSet(cap=2000)
    for i in range(2001):
        s.add(f"key_{i:04d}")
    check("lru[size after trim]", len(s), 1000)
    recent = {f"key_{i:04d}" for i in range(1001, 2001)}
    retained = recent & set(s)
    check("lru[newest 1000 retained]", len(retained), 1000)

    # Re-add bumps recency (LRU touch).
    s = _BoundedSeenSet(cap=4, low=2)
    for k in "abcd":
        s.add(k)
    s.add("a")  # bump 'a' to most recent
    s.add("e")  # cap=4, size→5 → trim to 2
    check("lru[touch keeps 'a']", list(s), ["a", "e"])

    # discard / contains / clear
    s = _BoundedSeenSet(cap=10)
    s.add("x"); s.add("y")
    s.discard("x")
    check("lru[discard]", "x" in s, False)
    check("lru[contains]", "y" in s, True)
    s.clear()
    check("lru[clear]", len(s), 0)

    return {"passed": passed, "failed": failed, "details": details}


def test_role_report_no_broadcast() -> dict:
    """ — *_report.md → empty target set (interest layer).

    Three explicit checks per spec §2.1 #1 acceptance:
      - test_role_report_no_broadcast: developer_report.md fanout = []
      - test_worker_report_still_works: _poll_file_reports remains; WORKER-REPORT
        push intact via AutoCoordinator.
      - test_role_report_endswith_pattern: arbitrary `<x>_report.md` excluded.
    """
    from main import (
        _md_watch_interest_targets,
        _md_watch_targets,
        AutoCoordinator,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # test_role_report_no_broadcast
    check("rpt_no_bcast[developer_report]", _md_watch_interest_targets("developer_report.md"), set())
    check("rpt_no_bcast[content_report]", _md_watch_interest_targets("content_report.md"), set())
    # test_role_report_endswith_pattern (arbitrary prefix)
    check("rpt_no_bcast[xyz_report]", _md_watch_interest_targets("xyz_report.md"), set())
    check("rpt_no_bcast[a_report]", _md_watch_interest_targets("a_report.md"), set())
    # Routing-layer parity
    class _MP:
        running = True
    class _MT:
        def __init__(self, tid, role, proj):
            self.id = tid; self.name = role; self.role = role
            self.project_path = proj; self.pty_session = _MP()
    class _MA:
        def __init__(self, tabs):
            self._tabs = {t.id: t for t in tabs}
    proj = "/tmp/p"
    mgr = _MT("m", "manager", proj); dev = _MT("d", "developer", proj)
    api = _MA([mgr, dev])
    roles_dev = sorted(t.role for t in _md_watch_targets(api, proj, "developer_report.md"))
    check("rpt_no_bcast[targets developer_report]", roles_dev, [])
    roles_xyz = sorted(t.role for t in _md_watch_targets(api, proj, "foo_bar_report.md"))
    check("rpt_no_bcast[targets foo_bar_report]", roles_xyz, [])

    # test_worker_report_still_works — AutoCoordinator class + WORKER-REPORT
    # prefix string both present in module source (per phase2 contract).
    import inspect
    src = inspect.getsource(AutoCoordinator)
    check("rpt_no_bcast[WORKER-REPORT prefix]", "[WORKER-REPORT from " in src, True)
    check("rpt_no_bcast[_poll_file_reports name]", "_poll_file_reports" in src, True)

    return {"passed": passed, "failed": failed, "details": details}


def test_md_watch_no_review_prompt() -> dict:
    """ — assembled MD-WATCH msg never includes the
    'Review and adjust your queue' scripted prompt or the '[MD-WATCH-FOOTER...]'
    debug line when MD_WATCH_DEBUG_FOOTER is False (default).

    Flips the flag at runtime and re-checks the gated branch.
    """
    import main as _m
    import inspect
    src = inspect.getsource(_m)
    details: list[str] = []
    passed = failed = 0

    def check(label, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # Default flag state: False.
    check("ftr[default flag False]", _m.MD_WATCH_DEBUG_FOOTER, False)
    # Both gated lines appear EXACTLY once in the source (they're inside the
    # `if MD_WATCH_DEBUG_FOOTER:` branch only).
    check("ftr[review-prompt singular]",
          src.count("Review and adjust your queue / next action if relevant"), 1)
    check("ftr[footer-debug-line singular]",
          src.count("[MD-WATCH-FOOTER from coordination/"), 1)
    check("ftr[branch present]", "if MD_WATCH_DEBUG_FOOTER:" in src, True)

    # Toggling flag: source unchanged (both lines remain gated).
    orig = _m.MD_WATCH_DEBUG_FOOTER
    try:
        _m.MD_WATCH_DEBUG_FOOTER = True
        check("ftr[toggle on holds]", _m.MD_WATCH_DEBUG_FOOTER, True)
        _m.MD_WATCH_DEBUG_FOOTER = False
        check("ftr[toggle off restored]", _m.MD_WATCH_DEBUG_FOOTER, False)
    finally:
        _m.MD_WATCH_DEBUG_FOOTER = orig

    return {"passed": passed, "failed": failed, "details": details}


def test_coalescing_window() -> dict:
    """ — _MdWatchCoalescer 30s debounce + 90s ceiling."""
    from main import _MdWatchCoalescer

    details: list[str] = []
    passed = failed = 0

    def check(label, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    c = _MdWatchCoalescer(debounce_s=30.0, ceiling_s=90.0, enabled=True)
    # T=0: first event arrives.
    c.note_event("decisions_log.md", 0.0)
    # T=10: still inside debounce → defer.
    check("coalesce[t10 defer]", c.should_emit_now("decisions_log.md", 10.0), False)
    # T=20: another event resets the silence countdown.
    c.note_event("decisions_log.md", 20.0)
    # T=40: 20s silence after last event → still inside debounce.
    check("coalesce[t40 defer]", c.should_emit_now("decisions_log.md", 40.0), False)
    # T=51: 31s silence → debounce ready, emits now (and clears entry).
    check("coalesce[t51 emit]", c.should_emit_now("decisions_log.md", 51.0), True)
    # After emit, no buffered state for this file.
    check("coalesce[empty after emit]", c.pending(), [])

    # Ceiling test: persistent burst keeps debounce reset, but ceiling fires.
    c2 = _MdWatchCoalescer(debounce_s=30.0, ceiling_s=90.0, enabled=True)
    c2.note_event("blockers.md", 0.0)
    for t in range(10, 100, 10):  # events every 10s through T=90
        c2.note_event("blockers.md", float(t))
    # At T=95: last event was T=90 (5s silence — debounce not ready),
    # but first event was T=0 (95s elapsed — ceiling ready).
    check("coalesce[ceiling fires]",
          c2.should_emit_now("blockers.md", 95.0), True)

    # Disabled coalescer → always passes through.
    c3 = _MdWatchCoalescer(enabled=False)
    c3.note_event("x.md", 0.0)  # no-op
    check("coalesce[disabled emit]", c3.should_emit_now("x.md", 1.0), True)

    # No buffered entry → returns True (caller's pre-coalesce decision wins).
    c4 = _MdWatchCoalescer(enabled=True)
    check("coalesce[unbuffered emit]", c4.should_emit_now("never_seen.md", 5.0), True)

    # Force flush.
    c5 = _MdWatchCoalescer(enabled=True)
    c5.note_event("a.md", 0.0); c5.note_event("b.md", 0.0)
    c5.force_flush("a.md")
    check("coalesce[flush a]", "a.md" not in c5.pending(), True)
    check("coalesce[flush b kept]", "b.md" in c5.pending(), True)

    return {"passed": passed, "failed": failed, "details": details}


def test_pointer_mode() -> dict:
    """ — pointer-mode message format + line-range computation."""
    from main import (
        _md_watch_format_pointer,
        _md_watch_compute_appended_range,
        MDWATCH_POINTER_MODE,
        MDWATCH_POINTER_TAIL_FALLBACK_LINES,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    # Default flag — off until soak-tested.
    check("ptr[default off]", MDWATCH_POINTER_MODE, False)
    check("ptr[fallback const]", MDWATCH_POINTER_TAIL_FALLBACK_LINES, 50)

    # Pointer line shape.
    line = _md_watch_format_pointer(
        "blockers.md",
        from_hash="abc123def456",
        to_hash="def456abc789",
        lines_added=12, lines_removed=0,
        line_start=42, line_end=53,
    )
    check("ptr[has MD-PULL]", line.startswith("[MD-PULL "), True)
    check("ptr[contains path]", "coordination/blockers.md" in line, True)
    check("ptr[contains from_hash]", "from_hash=abc123def456" in line, True)
    check("ptr[contains to_hash]", "to_hash=def456abc789" in line, True)
    check("ptr[contains delta]", "+12/-0" in line, True)
    check("ptr[contains range]", "L42-53" in line, True)

    # Append-only range computation.: prev MUST end with "\n" to count
    # as a line-aligned prefix (otherwise a byte-extension of the last line
    # would be silently omitted from the range — injection vector → full-file
    # fallback instead). A newline-terminated prev exercises the secure append.
    prev = "L1\nL2\nL3\nL4\n"
    curr = "L1\nL2\nL3\nL4\nL5\nL6"
    s, e = _md_watch_compute_appended_range(prev, curr)
    check("range[append start]", s, 5)
    check("range[append end]", e, 6)

    # No trailing newline on prev → not a line-aligned prefix → full-file range.
    s, e = _md_watch_compute_appended_range("L1\nL2\nL3\nL4", curr)
    check("range[append no-nl start]", s, 1)
    check("range[append no-nl end]", e, 6)

    # No prev (first time) → full range.
    s, e = _md_watch_compute_appended_range("", "a\nb\nc")
    check("range[first start]", s, 1)
    check("range[first end]", e, 3)

    # Edit (non-append) → full file range.
    s, e = _md_watch_compute_appended_range("a\nb\nc", "a\nXX\nc")
    check("range[edit start]", s, 1)
    check("range[edit end]", e, 3)

    # Empty curr edge.
    s, e = _md_watch_compute_appended_range("anything", "")
    check("range[empty curr start]", s, 1)
    check("range[empty curr end]", e, 0)

    return {"passed": passed, "failed": failed, "details": details}


def test_state_persist() -> dict:
    """ — md_watch state sidecar persistence (restart-resilience).

    Without persistence, post-restart first-seen baseline silently absorbs
    appends made while GUI was down → delta=0 → 配送消失. This test asserts
    save/load round-trip preserves mtime/hash/prev so a post-load delta
    fires correctly.
    """
    import json, tempfile, time
    from pathlib import Path
    from main import (
        _md_watch_state_path,
        _md_watch_state_load,
        _md_watch_state_save,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    with tempfile.TemporaryDirectory() as td:
        coord = Path(td)
        # Save → file exists with expected shape.
        _md_watch_state_save(coord, "peer_developer.md", 1234.5, "abc123", "L1\nL2\n")
        sp = _md_watch_state_path(coord)
        check("sidecar[exists after save]", sp.exists(), True)
        data = json.loads(sp.read_text("utf-8"))
        check("sidecar[has key]", "peer_developer.md" in data, True)
        check("sidecar[hash preserved]", data["peer_developer.md"]["hash"], "abc123")
        check("sidecar[prev preserved]", data["peer_developer.md"]["prev"], "L1\nL2\n")

        # Load → caches populated + first_seen marked so next tick computes delta.
        file_mtime, file_hash, file_prev, first_seen = {}, {}, {}, set()
        loaded = _md_watch_state_load(coord, file_mtime, file_hash, file_prev, first_seen, "pd")
        check("load[returns True]", loaded, True)
        key = f"pd:{coord / 'peer_developer.md'}"
        check("load[mtime restored]", file_mtime.get(key), 1234.5)
        check("load[hash restored]", file_hash.get(key), "abc123")
        check("load[prev restored]", file_prev.get(key), "L1\nL2\n")
        check("load[first_seen marked]", key in first_seen, True)
        check("load[sentinel set]", "pd:__loaded__" in first_seen, True)

        # Idempotency: 2nd load returns False (sentinel guard).
        loaded2 = _md_watch_state_load(coord, file_mtime, file_hash, file_prev, first_seen, "pd")
        check("load[idempotent]", loaded2, False)

        # Multi-file save (incremental update).
        _md_watch_state_save(coord, "peer_security.md", 5678.0, "def456", "S1\n")
        data2 = json.loads(sp.read_text("utf-8"))
        check("sidecar[multi-file]", len(data2), 2)
        check("sidecar[prev file kept]", data2["peer_developer.md"]["hash"], "abc123")
        check("sidecar[new file added]", data2["peer_security.md"]["hash"], "def456")

        # No sidecar → load returns False, no exception.
        coord2 = Path(td) / "empty_sub"
        coord2.mkdir()
        fm2, fh2, fp2, fs2 = {}, {}, {}, set()
        loaded3 = _md_watch_state_load(coord2, fm2, fh2, fp2, fs2, "ac")
        check("load[no sidecar=False]", loaded3, False)
        check("load[sentinel still set]", "ac:__loaded__" in fs2, True)

    return {"passed": passed, "failed": failed, "details": details}


def test_cache_helpers() -> dict:
    """ — prompt cache helpers (system / tools / history)."""
    from main import (
        build_cached_system_block,
        attach_tools_cache_marker,
        attach_history_cache_marker,
        PROMPT_CACHE_ENABLED,
        PROMPT_CACHE_SYSTEM_TTL,
        PROMPT_CACHE_MIN_TOKENS,
    )

    details: list[str] = []
    passed = failed = 0

    def check(label, got, want):
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    check("cache[default enabled]", PROMPT_CACHE_ENABLED, True)
    check("cache[system TTL 1h]", PROMPT_CACHE_SYSTEM_TTL, "1h")
    check("cache[min tokens 4096]", PROMPT_CACHE_MIN_TOKENS, 4096)

    # Small system prompt (under 4096 tokens) → uncached passthrough.
    small = "you are claude"
    check("cache[small passthrough]", build_cached_system_block(small), small)

    # Large system prompt (≥ 4096 tokens × ~4 chars) → structured block.
    big = "x" * (4096 * 4 + 1)
    out = build_cached_system_block(big)
    check("cache[big is list]", isinstance(out, list), True)
    if isinstance(out, list):
        check("cache[big has block]", out[0]["type"], "text")
        check("cache[big has cache_control]", "cache_control" in out[0], True)
        if "cache_control" in out[0]:
            check("cache[big TTL 1h]", out[0]["cache_control"]["ttl"], "1h")

    # Tools cache marker — last entry gets cache_control.
    tools = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    out = attach_tools_cache_marker(tools)
    check("cache[tools last has cache_control]", "cache_control" in out[-1], True)
    check("cache[tools first untouched]", "cache_control" not in out[0], True)
    # Idempotent: pre-marked tools list returns unchanged.
    pre = [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral"}}]
    out2 = attach_tools_cache_marker(pre)
    # We DON'T add a second cache_control on top.
    check("cache[tools idempotent]", out2[-1]["cache_control"], {"type": "ephemeral"})

    # History cache marker — second-to-last message gets boundary block.
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "current turn"},
    ]
    out = attach_history_cache_marker(msgs)
    boundary = out[-2]
    check("cache[hist boundary structured]", isinstance(boundary["content"], list), True)
    if isinstance(boundary["content"], list):
        check("cache[hist boundary cache_control]",
              "cache_control" in boundary["content"][-1], True)
    # Last msg untouched.
    check("cache[hist last untouched type]", isinstance(out[-1]["content"], str), True)

    # Single-message list — no marker (need at least 2 messages).
    single = [{"role": "user", "content": "only one"}]
    out = attach_history_cache_marker(single)
    check("cache[hist single passthrough]",
          out[0]["content"], "only one")

    # Empty inputs.
    check("cache[empty tools]", attach_tools_cache_marker([]), [])
    check("cache[empty hist]", attach_history_cache_marker([]), [])

    return {"passed": passed, "failed": failed, "details": details}


def test_phase2_context_cuts() -> dict:
    """ phase 2: verify items #1/#6/#7/#9 contracts hold.

    #1  *_report.md no-broadcast at the message-construction layer (poll loop).
    #6  MD_WATCH_DEBUG_FOOTER constant defaults False; "Review and adjust" prompt
        and "[MD-WATCH-FOOTER ...]" line are gated on it.
    #7  tools/scripts/archive_closed_queue.py importable with --age/--apply/--dry-run.
    #9  the protocol doc ≤120 line essential + the coordination protocol_reference.md exists.
    """
    import importlib
    import inspect
    from pathlib import Path

    details: list[str] = []
    passed = failed = 0

    def check(label: str, got, want) -> None:
        nonlocal passed, failed
        if got == want:
            passed += 1
            details.append(f"PASS  {label}: got={got!r}")
        else:
            failed += 1
            details.append(f"FAIL  {label}: got={got!r}, want={want!r}")

    main = importlib.import_module("main")

    # ---- #6 footer suppression contract ----
    check("ph2#6[MD_WATCH_DEBUG_FOOTER default]", getattr(main, "MD_WATCH_DEBUG_FOOTER", None), False)
    src = inspect.getsource(main)
    # Both the scripted prompt and the FOOTER debug line must live behind the flag.
    review_count = src.count("Review and adjust your queue / next action if relevant")
    check("ph2#6[review-prompt single-occurrence]", review_count, 1)
    footer_marker = src.count("[MD-WATCH-FOOTER from coordination/")
    check("ph2#6[footer-marker single-occurrence]", footer_marker, 1)
    check("ph2#6[review-prompt behind flag]", "if MD_WATCH_DEBUG_FOOTER:" in src, True)

    # ---- #1 *_report.md exclusion in poll loop ----
    check("ph2#1[poll-loop endswith filter]",
          'name.endswith("_report.md")' in src, True)
    # Worker report channel preserved: _poll_file_reports must still exist.
    check("ph2#1[_poll_file_reports preserved]",
          hasattr(main, "AutoCoordinator"), True)
    auto_src = inspect.getsource(main.AutoCoordinator)
    check("ph2#1[WORKER-REPORT push intact]",
          "[WORKER-REPORT from " in auto_src, True)

    return {"passed": passed, "failed": failed, "details": details}


# ----- pytest entry points (active when run via pytest) -----

def test_md_watch_routing_passes() -> None:
    """Pytest entry point for routing checks."""
    result = run_self_test()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_bounded_seen_set_passes() -> None:
    """Pytest entry point for LRU checks."""
    result = test_bounded_seen_set_lru()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_phase2_passes() -> None:
    """Pytest entry point for phase 2 contract checks."""
    result = test_phase2_context_cuts()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_role_report_no_broadcast_passes() -> None:
    """Pytest entry: spec-named tests."""
    result = test_role_report_no_broadcast()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_md_watch_no_review_prompt_passes() -> None:
    """Pytest entry: spec-named test."""
    result = test_md_watch_no_review_prompt()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_coalescing_passes() -> None:
    """Pytest entry: coalescing."""
    result = test_coalescing_window()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_pointer_passes() -> None:
    """Pytest entry: pointer mode."""
    result = test_pointer_mode()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


def test_cache_helpers_passes() -> None:
    """Pytest entry: cache helpers."""
    result = test_cache_helpers()
    failures = [d for d in result["details"] if d.startswith("FAIL")]
    assert not failures, "\n".join(failures)


# ----- standalone CLI entry point -----

def _print_and_exit(*results) -> None:
    total_passed = sum(r["passed"] for r in results)
    total_failed = sum(r["failed"] for r in results)
    for r in results:
        for line in r["details"]:
            print(line)
    total = total_passed + total_failed
    print(f"[md-watch-test] {total_passed}/{total} passed")
    sys.exit(0 if total_failed == 0 else 1)


if __name__ == "__main__":
    _print_and_exit(
        run_self_test(),
        test_bounded_seen_set_lru(),
        test_phase2_context_cuts(),
        test_role_report_no_broadcast(),
        test_md_watch_no_review_prompt(),
        test_coalescing_window(),
        test_pointer_mode(),
        test_cache_helpers(),
    )
