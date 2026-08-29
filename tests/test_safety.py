from gftrade.clients.rugcheck import parse_lp_locked_pct
from gftrade.discovery.safety import SafetyChecker, SafetyReport


class FakeRpc:
    def __init__(self, mint_info, largest=None, fail=False):
        self.mint_info = mint_info
        self.largest = largest or []
        self.fail = fail
        self.mint_info_calls = 0

    async def get_mint_info(self, mint):
        if self.fail:
            raise ConnectionError("rpc down")
        self.mint_info_calls += 1
        return self.mint_info

    async def get_token_largest_accounts(self, mint):
        return self.largest


class FakeRugCheck:
    def __init__(self, pct, fail=False):
        self.pct = pct
        self.fail = fail

    async def lp_locked_pct(self, mint):
        if self.fail:
            raise ConnectionError("rugcheck down")
        return self.pct


from gftrade import constants

CLEAN_MINT_INFO = {"decimals": 9, "supply": 1000,
                   "mint_authority": None, "freeze_authority": None,
                   "owner_program": constants.TOKEN_PROGRAM_ID}


def accounts(*amounts):
    return [{"address": f"acc{i}", "amount": str(a)} for i, a in enumerate(amounts)]


def clean_rpc():
    # top10 excl. largest = 100/1000 = 10%
    return FakeRpc(CLEAN_MINT_INFO, largest=accounts(500, 40, 30, 20, 10))


async def test_clean_locked_token_passes_strict():
    report = await SafetyChecker(clean_rpc(), FakeRugCheck(100.0)).check("MINT")
    assert report.mint_renounced is True
    assert report.freeze_none is True
    assert report.top10_pct == 10.0
    assert report.lp_locked_pct == 100.0
    assert report.passes(strict=True)


async def test_unlocked_lp_fails_both_modes():
    report = await SafetyChecker(clean_rpc(), FakeRugCheck(5.0)).check("MINT")
    assert report.lp_locked_pct == 5.0
    assert not report.passes(strict=True)
    assert not report.passes(strict=False)
    assert "LP 🚨" in report.line()


async def test_unknown_lp_strict_rejects_lenient_allows():
    for rugcheck in (FakeRugCheck(None), FakeRugCheck(0, fail=True), None):
        report = await SafetyChecker(clean_rpc(), rugcheck).check("MINT")
        assert report.lp_locked_pct is None
        assert not report.passes(strict=True)
        assert report.passes(strict=False)


async def test_token_2022_mint_fails_both_modes():
    """Non-standard token programs can carry sell-trap extensions our
    authority checks can't see — treated as known-bad, not unknown."""
    rpc = FakeRpc({**CLEAN_MINT_INFO,
                   "owner_program": "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"},
                  largest=accounts(500, 10))
    report = await SafetyChecker(rpc, FakeRugCheck(100.0)).check("MINT")
    assert report.standard_token is False
    assert not report.passes(strict=True)
    assert not report.passes(strict=False)
    assert "Token-2022" in report.line()


async def test_missing_owner_program_is_unknown_not_bad():
    info = dict(CLEAN_MINT_INFO)
    del info["owner_program"]
    rpc = FakeRpc(info, largest=accounts(500, 10))
    report = await SafetyChecker(rpc, FakeRugCheck(100.0)).check("MINT")
    assert report.standard_token is None
    assert not report.passes(strict=True)   # unproven -> strict rejects
    assert report.passes(strict=False)      # but it's not known-bad


async def test_active_freeze_authority_fails_both_modes():
    rpc = FakeRpc({**CLEAN_MINT_INFO, "freeze_authority": "SomeAuthorityKey"},
                  largest=accounts(500, 10))
    report = await SafetyChecker(rpc, FakeRugCheck(100.0)).check("MINT")
    assert report.freeze_none is False
    assert not report.passes(strict=True)
    assert not report.passes(strict=False)


async def test_active_mint_authority_fails_both_modes():
    rpc = FakeRpc({**CLEAN_MINT_INFO, "mint_authority": "DevKey"},
                  largest=accounts(500, 10))
    report = await SafetyChecker(rpc, FakeRugCheck(100.0)).check("MINT")
    assert report.mint_renounced is False
    assert not report.passes(strict=False)


async def test_concentrated_holders_fail_strict():
    rpc = FakeRpc(CLEAN_MINT_INFO, largest=accounts(400, 200, 150, 100))  # 45%
    report = await SafetyChecker(rpc, FakeRugCheck(100.0)).check("MINT")
    assert report.top10_pct == 45.0
    assert not report.passes(strict=True)


async def test_rpc_failure_unknown_strict_rejects_lenient_allows():
    report = await SafetyChecker(FakeRpc(None, fail=True), FakeRugCheck(100.0)).check("M")
    assert report.mint_renounced is None
    assert report.error
    assert not report.passes(strict=True)
    assert report.passes(strict=False)


async def test_complete_reports_are_cached():
    rpc = clean_rpc()
    checker = SafetyChecker(rpc, FakeRugCheck(100.0))
    await checker.check("MINT")
    await checker.check("MINT")
    assert rpc.mint_info_calls == 1


async def test_incomplete_reports_get_short_ttl():
    rpc = clean_rpc()
    checker = SafetyChecker(rpc, FakeRugCheck(None))
    await checker.check("MINT")
    _, _, ttl = checker._cache["MINT"]
    assert ttl == 120


def test_safety_line_renders_unknowns():
    assert "❓" in SafetyReport(mint="x").line()
    good = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                        top10_pct=12.0, lp_locked_pct=97.0)
    line = good.line()
    assert "renounced" in line and "12%" in line and "LP 🔒 97%" in line


def test_parse_lp_locked_pct_shapes():
    # normal shape: max across markets
    data = {"markets": [{"lp": {"lpLockedPct": 42.5}}, {"lp": {"lpLockedPct": 99.9}}]}
    assert parse_lp_locked_pct(data) == 99.9
    # risks-list fallback names an unlocked LP
    assert parse_lp_locked_pct({"risks": [{"name": "LP Unlocked"}]}) == 0.0
    # unknown shapes -> None, never a guess
    assert parse_lp_locked_pct({}) is None
    assert parse_lp_locked_pct({"markets": [{"lp": {}}]}) is None
    assert parse_lp_locked_pct(None) is None
    assert parse_lp_locked_pct({"markets": [{"lp": {"lpLockedPct": "high"}}]}) is None
