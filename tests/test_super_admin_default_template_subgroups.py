import os
import tempfile
import unittest

from review_processor.repository import ReviewRepository


class SuperAdminDefaultTemplateSubgroupsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = temp.name
        temp.close()
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.unlink(self.db_path))
        self.repository = ReviewRepository(db_path=self.db_path)

    def test_can_add_and_delete_custom_default_template_subgroup(self) -> None:
        self.repository.add_default_template_subgroup(
            group_id="positive",
            subgroup="Новая группа тест",
        )
        listed = self.repository.list_default_template_subgroups(group_id="positive")
        names = [str(item.get("subgroup") or "") for item in listed]
        self.assertIn("Новая группа тест", names)

        deleted = self.repository.delete_default_template_subgroup(
            group_id="positive",
            subgroup="Новая группа тест",
        )
        self.assertTrue(deleted)
        listed_after = self.repository.list_default_template_subgroups(group_id="positive")
        names_after = [str(item.get("subgroup") or "") for item in listed_after]
        self.assertNotIn("Новая группа тест", names_after)

    def test_can_delete_seeded_default_subgroup(self) -> None:
        self.repository.ensure_default_template_subgroups(
            [
                {"group_id": "positive", "subgroup": "Вкус"},
                {"group_id": "positive", "subgroup": "Материал"},
            ]
        )
        deleted = self.repository.delete_default_template_subgroup(
            group_id="positive",
            subgroup="Вкус",
        )
        self.assertTrue(deleted)
        listed_after = self.repository.list_default_template_subgroups(group_id="positive")
        names_after = [str(item.get("subgroup") or "") for item in listed_after]
        self.assertNotIn("Вкус", names_after)

    def test_general_subgroup_stays_first_after_sorting(self) -> None:
        self.repository.ensure_default_template_subgroups(
            [
                {"group_id": "positive", "subgroup": "Материал"},
                {"group_id": "positive", "subgroup": "Общий"},
                {"group_id": "positive", "subgroup": "Вкус"},
            ]
        )
        listed = self.repository.list_default_template_subgroups(group_id="positive")
        names = [str(item.get("subgroup") or "") for item in listed]
        names.sort(key=lambda value: (0 if value == "Общий" else 1, value.casefold()))
        self.assertEqual(names[0], "Общий")

    def test_rename_subgroup_preserves_subgroup_id(self) -> None:
        created = self.repository.add_default_template_subgroup(
            group_id="positive",
            subgroup="Старое имя",
        )
        original_subgroup_id = str(created.get("subgroup_id") or "")
        self.assertTrue(bool(original_subgroup_id))

        renamed = self.repository.rename_default_template_subgroup(
            group_id="positive",
            subgroup="Старое имя",
            new_subgroup="Новое имя",
        )
        self.assertTrue(renamed)

        old_row = self.repository.get_default_template_subgroup(
            group_id="positive",
            subgroup="Старое имя",
        )
        self.assertIsNone(old_row)
        new_row = self.repository.get_default_template_subgroup(
            group_id="positive",
            subgroup="Новое имя",
        )
        self.assertIsNotNone(new_row)
        self.assertEqual(str((new_row or {}).get("subgroup_id") or ""), original_subgroup_id)

    def test_bulk_add_default_template_variants_deduplicates(self) -> None:
        added = self.repository.add_default_template_variants_bulk(
            group_id="positive",
            subgroup="Массовая загрузка",
            templates=[
                "Шаблон А",
                "Шаблон Б",
                "Шаблон А",
                "   ",
            ],
        )
        self.assertEqual(added, 2)

        listed = self.repository.list_default_template_variants(
            group_id="positive",
            subgroup="Массовая загрузка",
        )
        texts = [str(item.get("template_text") or "") for item in listed]
        self.assertEqual(sorted(texts), ["Шаблон А", "Шаблон Б"])

        added_again = self.repository.add_default_template_variants_bulk(
            group_id="positive",
            subgroup="Массовая загрузка",
            templates=[
                "Шаблон Б",
                "Шаблон В",
            ],
        )
        self.assertEqual(added_again, 1)


if __name__ == "__main__":
    unittest.main()
