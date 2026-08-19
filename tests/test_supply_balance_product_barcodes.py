"""Tests for Остатки product ШК collection from FBS orders."""

from review_processor.repository import ReviewRepository


def test_get_wb_fbs_barcodes_by_product_id_maps_article_and_nm() -> None:
    repo = ReviewRepository.__new__(ReviewRepository)
    repo.get_product_id_by_article_map = (  # type: ignore[method-assign]
        lambda *, user_id: {"ART-1": 10, "art-1": 10, "111": 10, "ART-2": 20, "art-2": 20}
    )
    repo._sql = lambda q: q  # type: ignore[method-assign]
    repo._row_to_dict = lambda r: dict(r)  # type: ignore[method-assign]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            class _Cur:
                def fetchall(self_inner):
                    return [
                        {
                            "article": "ART-1",
                            "nm_id": "111",
                            "skus_json": '["4601234567890", "4601234567890", "ART-1"]',
                        },
                        {
                            "article": "ART-2",
                            "nm_id": "",
                            "skus_json": '["999"]',
                        },
                    ]

            return _Cur()

    repo._connect = lambda: _Conn()  # type: ignore[method-assign]
    got = ReviewRepository.get_wb_fbs_barcodes_by_product_id(repo, user_id=1)
    assert got[10] == ["4601234567890", "ART-1"]
    assert got[20] == ["999"]
