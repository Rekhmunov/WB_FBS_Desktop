# -*- coding: utf-8 -*-
import unittest

from PyQt5.QtWidgets import (
    QApplication,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QTableWidgetSelectionRange,
)

from app.ui.text_copy import (
    enable_label_copy,
    selected_text_from_widget,
    table_selection_text,
)


class TextCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_label_full_text_when_nothing_selected(self) -> None:
        lab = QLabel("Заказ 12345")
        enable_label_copy(lab)
        self.assertEqual(selected_text_from_widget(lab), "Заказ 12345")

    def test_photo_label_is_not_copyable(self) -> None:
        lab = QLabel()
        lab.setPixmap(lab.style().standardPixmap(lab.style().SP_MessageBoxInformation))
        enable_label_copy(lab)
        self.assertEqual(selected_text_from_widget(lab), "")

    def test_table_selection_text(self) -> None:
        table = QTableWidget(2, 2)
        table.setItem(0, 0, QTableWidgetItem("a"))
        table.setItem(0, 1, QTableWidgetItem("b"))
        table.setItem(1, 0, QTableWidgetItem("c"))
        table.setItem(1, 1, QTableWidgetItem("d"))
        table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 1, 1), True)
        self.assertEqual(table_selection_text(table), "a\tb\nc\td")


if __name__ == "__main__":
    unittest.main()
