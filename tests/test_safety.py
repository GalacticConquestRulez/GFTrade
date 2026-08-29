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


def accounts(*amounts):
    return [{"address": f"acc{i}", "amount": str(a)} for i, a in enumerate(amounts)]


async def test_clean_token_passes_strict():
    rpc = FakeRpc(
        {"decimals": 9, "supply": 1000, "mint_authority": None, "freeze_authority": None},
        largest=accounts(500, 40, 30, 20, 10),  # top10 excl. largest = 100/1000 = 10%
    )
    report = await SafetyChecker(rpc).check("MINT")
    assert report.mint_renounced is True
    assert report.freeze_none is True
    assert report.top10_pct == 10.0
    assert report.passes(strict=True)


async def test_active_freeze_authority_fails_both_modes():
    rpc = FakeRpc(
        {"decimals": 9, "supply": 1000, "mint_authority": None,
         "freeze_authority": "SomeAuthorityKey"},
        largest=accounts(500, 10),
    )
    report = await SafetyChecker(rpc).check("MINT")
    assert report.freeze_none is False
    assert not report.passes(strict=True)
    assert not report.passes(strict=False)


async def test_concentrated_holders_fail_strict():
    rpc = FakeRpc(
        {"decimals": 9, "supply": 1000, "mint_authority": None, "freeze_authority": None},
        largest=accounts(400, 200, 150, 100),  # 45% excl. largest
    )
    report = await SafetyChecker(rpc).check("MINT")
    assert report.top10_pct == 45.0
    assert not report.passes(strict=True)


async def test_rpc_failure_unknown_strict_rejects_lenient_allows():
    rpc = FakeRpc(None, fail=True)
    report = await SafetyChecker(rpc).check("MINT")
    assert report.mint_renounced is None
    assert report.error
    assert not report.passes(strict=True)
    assert report.passes(strict=False)


async def test_reports_are_cached():
    rpc = FakeRpc(
        {"decimals": 9, "supply": 1000, "mint_authority": None, "freeze_authority": None},
        largest=accounts(500, 10),
    )
    checker = SafetyChecker(rpc)
    await checker.check("MINT")
    await checker.check("MINT")
    assert rpc.mint_info_calls == 1


def test_safety_line_renders_unknowns():
    assert "❓" in SafetyReport(mint="x").line()
    good = SafetyReport(mint="x", mint_renounced=True, freeze_none=True, top10_pct=12.0)
    line = good.line()
    assert "renounced" in line and "12%" in line
