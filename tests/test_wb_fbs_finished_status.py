"""WB FBS finished/cancelled status labels."""

from review_processor.wb_fbs import (
    TAB_CANCELLED,
    TAB_FINISHED,
    cancel_reason_label,
    compute_tab,
    finished_status_label,
)


def test_sold_goes_to_finished_with_purchased_label():
    assert compute_tab(supplier_status="complete", wb_status="sold", is_archive=False) == TAB_FINISHED
    assert finished_status_label(wb_status="sold") == "Заказ выкуплен"


def test_canceled_by_client_stays_cancelled():
    assert (
        compute_tab(
            supplier_status="complete",
            wb_status="canceled_by_client",
            is_archive=False,
        )
        == TAB_CANCELLED
    )
    assert cancel_reason_label(wb_status="canceled_by_client") == "Отказ на ПВЗ"
    assert finished_status_label(wb_status="canceled_by_client") == ""


def test_defect_stays_cancelled():
    assert compute_tab(supplier_status="complete", wb_status="defect", is_archive=False) == TAB_CANCELLED
    assert cancel_reason_label(wb_status="defect") == "Найдены дефекты"


def test_early_declined_stays_cancelled():
    assert (
        compute_tab(
            supplier_status="new",
            wb_status="declined_by_client",
            is_archive=False,
        )
        == TAB_CANCELLED
    )
