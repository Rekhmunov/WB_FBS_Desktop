"""Schema-oriented checks for Ozon eTrN title-1 XML draft."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

from review_processor.ozon_etrn import (
    OZON_CONSIGNEE_EDO_GUID,
    OZON_CONSIGNEE_INN,
    OZON_CONSIGNEE_KPP,
    OZON_CONSIGNEE_NAME,
    _cargo_stats,
    _ozon_supply_number,
    build_ozon_cargoes_cache,
    build_ozon_etrn_xml,
)


def _build(**overrides):
    kwargs = dict(
        item={
            "supply_order_id": 123,
            "supply_order_number": "0123456789",
            "supplier_name": 'ООО "Тест"',
            "warehouse_name": "ХОРУГВИНО_РФЦ",
            "supply_date": "2026-08-15T14:30:00",
        },
        le={
            "full_name": 'ООО "Тест Поставщик"',
            "short_name": "Тест",
            "requisites": (
                "ИНН 7701234567 КПП 770101001 "
                "юр. адрес: 101000, г. Москва, ул. Ленина, д. 1"
            ),
            "signatories": "Иванов Иван Иванович",
            "phone": "+79991112233",
        },
        driver_name="Петров Пётр Петрович",
        driver_phone="+79001234567",
        driver_documents="ВУ 99 00 123456 выд. 01.02.2018",
        vehicle_line="GAZelle A123BC77",
        cargoes_json=[{"type": "PALLET", "content_type": "ITEM", "count": 2}],
        load_address="141580, Московская обл., г. Химки, ул. Заводская, д. 10",
        delivery_address="143420, Московская обл., г. Истра, ул. Складская, д. 5",
        carrier_text="ООО Перевозчик ИНН 5001002003 КПП 500101001",
        now=datetime(2026, 8, 6, 12, 30, 0),
    )
    kwargs.update(overrides)
    return build_ozon_etrn_xml(**kwargs)


def test_etrn_xml_core_schema_shape():
    root = ET.fromstring(_build())
    assert root.tag == "Файл"
    assert root.attrib["ВерсФорм"] == "5.01"
    assert OZON_CONSIGNEE_EDO_GUID in root.attrib["ИдФайл"]

    doc = root.find("Документ")
    assert doc is not None
    assert doc.attrib["КНД"] == "1110339"

    sod = doc.find("СодИнфГО")
    assert sod is not None

    # InfPol is last child of СодИнфГО and uses Значение (not Значен).
    children = list(sod)
    assert children[-1].tag == "ИнфПол"
    texts = children[-1].findall("ТекстИнф")
    assert {t.attrib.get("Идентиф") for t in texts} >= {"Orders", "ORDERS"}
    for t in texts:
        assert t.attrib.get("Значение") == "0123456789"
        assert "Значен" not in t.attrib

    # Delivery / loading use АдресРФ; legal address under Адрес uses АдрРФ.
    # Never emit АдрИнф/АдресИнф — Kontur treats that as foreign address type.
    assert sod.find("СвГП/АдресДостГр/АдресРФ") is not None
    assert sod.find("СвГП/АдресДостГр/АдрРФ") is None
    assert sod.find("СвГП/РекИдентГП/Адрес/АдрРФ") is not None
    assert sod.find("СвГП/РекИдентГП/Адрес/АдрРФ").attrib.get("КодРегион") == "77"
    assert sod.find("СвПогруз/ФАдресПогр/АдресРФ") is not None
    assert sod.find("СвГО/РекИдентГО/Адрес/АдрРФ") is not None
    assert sod.find(".//АдрИнф") is None
    assert sod.find(".//АдресИнф") is None

    # No empty GAR / phone stubs.
    assert sod.find(".//КодГАР") is None
    for phone in sod.findall(".//Тлф"):
        assert (phone.text or "").strip()

    # Required signer under Документ.
    signer = doc.find("Подписант")
    assert signer is not None
    assert signer.attrib.get("СтатПодп") == "1"
    assert signer.find("ФИО") is not None

    # Vehicle ownership + parameters required by schema.
    ts = sod.find("СвТС/ТС")
    assert ts is not None
    assert ts.attrib.get("ТипВлад") == "1"
    part = ts.find("ПарТС")
    assert part is not None
    assert part.attrib.get("Тип")
    assert part.attrib.get("Грузопод")
    assert part.attrib.get("Вместим")

    # Ozon consignee + cargo required fields.
    gp_ul = sod.find("СвГП/РекИдентГП/ИдСв/СвЮЛУч")
    assert gp_ul is not None
    assert gp_ul.attrib["НаимОрг"] == OZON_CONSIGNEE_NAME
    assert gp_ul.attrib["НаимОрг"] == 'ООО "ИНТЕРНЕТ РЕШЕНИЯ"'
    assert gp_ul.attrib["ИННЮЛ"] == OZON_CONSIGNEE_INN == "7704217370"
    assert gp_ul.attrib["КПП"] == OZON_CONSIGNEE_KPP == "997750001"
    gp_adr = sod.find("СвГП/РекИдентГП/Адрес/АдрРФ")
    assert gp_adr is not None
    assert gp_adr.attrib.get("Индекс") == "123112"
    assert gp_adr.attrib.get("КодРегион") == "77"
    assert gp_adr.attrib.get("Город") == "Москва"
    assert "Пресненская" in (gp_adr.attrib.get("Улица") or "")
    assert "наб" in (gp_adr.attrib.get("Улица") or "").lower()
    assert gp_adr.attrib.get("Дом") == "10"
    # УказГО / дата доставки ← supply_date из таблицы поставок.
    ukaz = sod.find("УказГО")
    assert ukaz is not None
    assert ukaz.attrib.get("ДатВрДостГр") == "15.08.2026T14:30:00+03:00"
    assert ukaz.attrib.get("НалКоорТочВрДост") == "1"
    op = sod.find("СвГруз/ОпГруз")
    assert op.attrib.get("КолМестГр") == "2"
    assert op.find("ПлМасГруз").attrib.get("МасБрутЗнач")
    assert sod.find("СвПогруз").attrib.get("МетОпрМасс") == "03"
    assert op.attrib.get("СостГруз") == "Без повреждений"
    assert op.attrib.get("СпУпак") == "Паллеты"


def test_etrn_delivery_datetime_from_supply_date_date_only():
    """Date-only supply_date → midnight MSK in ДатВрДостГр."""
    root = ET.fromstring(
        _build(
            item={
                "supply_order_id": 1,
                "supply_order_number": "1",
                "supplier_name": "X",
                "warehouse_name": "W",
                "supply_date": "2026-09-01",
            }
        )
    )
    ukaz = root.find("Документ/СодИнфГО/УказГО")
    assert ukaz is not None
    assert ukaz.attrib.get("ДатВрДостГр") == "01.09.2026T00:00:00+03:00"
    assert ukaz.attrib.get("НалКоорТочВрДост") == "1"


def test_ozon_supply_number_prefers_table_column():
    assert _ozon_supply_number(
        {"supply_order_id": 999, "supply_order_number": "020-111222333"}
    ) == "020-111222333"
    assert _ozon_supply_number(
        {
            "supply_order_id": 999,
            "supply_order_number": "",
            "raw_json": '{"order_number": "020-from-raw"}',
        }
    ) == "020-from-raw"
    assert _ozon_supply_number({"supply_order_id": 999, "supply_order_number": ""}) == "999"


def test_etrn_infpol_orders_uses_supply_number_from_main_table():
    """ИнфПол/ТекстИнф Идентиф=Orders → Значение = номер из основной таблицы ОЗОН."""
    root = ET.fromstring(
        _build(
            item={
                "supply_order_id": 555666,
                "supply_order_number": "020-987654321",
                "supplier_name": 'ООО "Тест"',
                "warehouse_name": "ХОРУГВИНО_РФЦ",
                "supply_date": "2026-08-15T14:30:00",
            }
        )
    )
    texts = root.findall("Документ/СодИнфГО/ИнфПол/ТекстИнф")
    by_id = {t.attrib.get("Идентиф"): t.attrib.get("Значение") for t in texts}
    assert by_id.get("Orders") == "020-987654321"
    assert by_id.get("ORDERS") == "020-987654321"
    # Must not put internal supply_order_id when table number is present.
    assert "555666" not in by_id.values()


def test_cargo_stats_transport_pallets_drive_kol_mest():
    """Транспортные паллеты → КолМестГр = число паллет, не коробок."""
    cache = build_ozon_cargoes_cache(
        flat_cargoes=[{"type": "BOX", "content_type": "MONO"}] * 41,
        supplies_cargoes=[
            {
                "supply_id": 1,
                "transport_cargoes": [
                    {"type": "PALLET", "transport_cargo_id": "a", "cargoes": [{}] * 11},
                    {"type": "PALLET", "transport_cargo_id": "b", "cargoes": [{}] * 30},
                ],
            }
        ],
    )
    stats = _cargo_stats(cache)
    assert stats["pallets"] == 2
    assert stats["boxes"] == 41
    assert stats["total_places"] == 2
    assert "2 палет" in stats["cargo_name"]
    assert "41" in stats["cargo_name"]

    root = ET.fromstring(_build(cargoes_json=cache))
    op = root.find("Документ/СодИнфГО/СвГруз/ОпГруз")
    assert op is not None
    assert op.attrib.get("КолМестГр") == "2"
    assert op.attrib.get("СпУпак") == "Паллеты"
    assert root.find("Документ/СодИнфГО/СвПогруз").attrib.get("КолМестПрием") == "2"


def test_parse_ru_address_suffix_embankment_street():
    """«Пресненская наб.» must populate Улица (type after name)."""
    from review_processor.ozon_etrn import OZON_CONSIGNEE_ADDRESS, _parse_ru_address

    parsed = _parse_ru_address(OZON_CONSIGNEE_ADDRESS)
    assert parsed["Индекс"] == "123112"
    assert parsed["Город"] == "Москва"
    assert parsed["Дом"] == "10"
    assert "Пресненская" in parsed["Улица"]
    assert "наб" in parsed["Улица"].lower()


def test_etrn_xml_empty_cargoes_still_has_required_mass_places():
    root = ET.fromstring(_build(cargoes_json=[]))
    op = root.find("Документ/СодИнфГО/СвГруз/ОпГруз")
    assert op is not None
    assert int(op.attrib["КолМестГр"]) >= 1
    assert int(op.find("ПлМасГруз").attrib["МасБрутЗнач"]) >= 1


def test_etrn_xml_incomplete_legal_address_still_adr_rf():
    """Unparseable legal address must stay АдрРФ, not АдрИнф (foreign)."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567 КПП 770101001 адрес: деревня БезИндекса, участок 7",
                "signatories": "Иванов Иван Иванович",
            },
            load_address="",
            delivery_address="склад без индекса",
        )
    )
    adr = root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрРФ")
    assert adr is not None
    assert root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрИнф") is None
    assert root.find(".//АдрИнф") is None
    assert root.find(".//АдресИнф") is None
    assert root.find("Документ/СодИнфГО/СвГП/АдресДостГр/АдресРФ") is not None
    assert root.find("Документ/СодИнфГО/СвПогруз/ФАдресПогр/АдресРФ") is not None


def test_etrn_shipper_address_from_legal_entity_not_load():
    """Грузоотправитель address = юр.лица.address (else requisites), not warehouse."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567 КПП 770101001",
                "address": "141200, Московская область, г. Пушкино, ул. Лесная, д. 5",
                "signatories": "Иванов Иван Иванович",
            },
            load_address=(
                "Московская область, Солнечногорский район, "
                "сельское поселение Пешковское, деревня Хоругвино, строение 32/2"
            ),
        )
    )
    adr = root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрРФ")
    assert adr is not None
    shipper_xml = ET.tostring(adr, encoding="unicode")
    assert "Хоругвино" not in shipper_xml
    assert "Пушкино" in shipper_xml or "Лесная" in shipper_xml
    # Load address still goes to ФАдресПогр.
    load_xml = ET.tostring(root.find("Документ/СодИнфГО/СвПогруз/ФАдресПогр"), encoding="unicode")
    assert "Хоругвино" in load_xml


def test_etrn_shipper_address_prefers_address_over_requisites():
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567 адрес: г. Москва, ул. Старая, д. 1",
                "address": "141200, г. Пушкино, ул. Новая, д. 9",
                "signatories": "Иванов Иван Иванович",
            }
        )
    )
    shipper_xml = ET.tostring(
        root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрРФ"),
        encoding="unicode",
    )
    assert "Новая" in shipper_xml or "Пушкино" in shipper_xml
    assert "Старая" not in shipper_xml


def test_etrn_shipper_uses_structured_legal_entity_fields():
    """СвГО takes structured юр.лица address fields without free-text parse."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567",
                "address": "ignored free text that would parse differently",
                "signatories": "Иванов Иван Иванович",
                "addr_index": "141200",
                "addr_region_code": "50",
                "addr_city": "Пушкино",
                "addr_street": "ул. Лесная",
                "addr_house": "5",
                "addr_corpus": "2",
                "addr_flat": "10",
            }
        )
    )
    adr = root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрРФ")
    assert adr is not None
    assert adr.attrib.get("Индекс") == "141200"
    assert adr.attrib.get("КодРегион") == "50"
    assert adr.attrib.get("Город") == "Пушкино"
    assert adr.attrib.get("Улица") == "ул. Лесная"
    assert adr.attrib.get("Дом") == "5"
    assert adr.attrib.get("Корпус") == "2"
    assert adr.attrib.get("Кварт") == "10"


def test_legal_entity_address_line_assembles_for_documents():
    from review_processor.repository import ReviewRepository

    line = ReviewRepository.legal_entity_address_line(
        {
            "addr_index": "141200",
            "addr_city": "Пушкино",
            "addr_street": "ул. Лесная",
            "addr_house": "5",
            "address": "legacy",
        }
    )
    assert line == "141200, г. Пушкино, ул. Лесная, д. 5"
    assert (
        ReviewRepository.legal_entity_address_line(
            {"address": "старый адрес одной строкой", "addr_city": ""}
        )
        == "старый адрес одной строкой"
    )


def test_etrn_consignee_addresses_are_russian_rf():
    root = ET.fromstring(_build())
    legal = root.find("Документ/СодИнфГО/СвГП/РекИдентГП/Адрес/АдрРФ")
    dost = root.find("Документ/СодИнфГО/СвГП/АдресДостГр/АдресРФ")
    assert legal is not None
    assert dost is not None
    assert root.find("Документ/СодИнфГО/СвГП//АдрИнф") is None
    assert root.find("Документ/СодИнфГО/СвГП//АдресИнф") is None
    assert legal.attrib.get("КодРегион") == "77"


def test_etrn_carrier_address_is_russian_rf():
    root = ET.fromstring(_build())
    adr = root.find("Документ/СодИнфГО/СвПер/Адрес/АдрРФ")
    assert adr is not None
    assert root.find("Документ/СодИнфГО/СвПер/Адрес/АдрИнф") is None
    assert adr.attrib.get("КодРегион")  # RF type needs region


def test_etrn_carrier_uses_structured_fields():
    """СвПер takes structured carrier org + address without free-text parse."""
    root = ET.fromstring(
        _build(
            carrier_text="ignored free text ООО Старый ИНН 1111111111",
            carrier_fields={
                "carrier_name": 'ООО "Новый Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_kpp": "500101001",
                "carrier_addr_index": "141580",
                "carrier_addr_region_code": "50",
                "carrier_addr_city": "Химки",
                "carrier_addr_street": "ул. Складская",
                "carrier_addr_house": "7",
            },
        )
    )
    org = root.find("Документ/СодИнфГО/СвПер/ИдСв/СвЮЛУч")
    assert org is not None
    assert org.attrib.get("НаимОрг") == 'ООО "Новый Перевозчик"'
    assert org.attrib.get("ИННЮЛ") == "5001002003"
    assert org.attrib.get("КПП") == "500101001"
    adr = root.find("Документ/СодИнфГО/СвПер/Адрес/АдрРФ")
    assert adr is not None
    assert adr.attrib.get("Индекс") == "141580"
    assert adr.attrib.get("КодРегион") == "50"
    assert adr.attrib.get("Город") == "Химки"
    assert adr.attrib.get("Улица") == "ул. Складская"
    assert adr.attrib.get("Дом") == "7"


def test_compose_carrier_line_for_documents():
    from review_processor.repository import ReviewRepository

    line = ReviewRepository.compose_carrier_line(
        {
            "carrier_name": 'ООО "Перевозчик"',
            "carrier_inn": "5001002003",
            "carrier_kpp": "500101001",
            "carrier_addr_index": "141580",
            "carrier_addr_city": "Химки",
            "carrier_addr_street": "ул. Складская",
            "carrier_addr_house": "7",
        }
    )
    assert 'ООО "Перевозчик"' in line
    assert "ИНН 5001002003" in line
    assert "КПП 500101001" in line
    assert "141580" in line
    assert "Химки" in line
    assert "ул. Складская" in line
    assert (
        ReviewRepository.carrier_line({"carrier": "старая строка", "carrier_name": ""})
        == "старая строка"
    )


def test_etrn_driver_uses_structured_vu_fields():
    """СвВодит takes structured VU fields without free-text parse."""
    root = ET.fromstring(
        _build(
            driver_documents="ignored free text ВУ 11 11 111111 выд. 01.01.2001",
            driver_fields={
                "doc_vu_series": "9900",
                "doc_vu_number": "123456",
                "doc_vu_date": "01.02.2018",
                "doc_inn_fl": "",
            },
        )
    )
    vod = root.find("Документ/СодИнфГО/СвВодит")
    assert vod is not None
    assert vod.attrib.get("СерВУ") == "9900"
    assert vod.attrib.get("НомВУ") == "123456"
    assert vod.attrib.get("ДатаВыдВУ") == "01.02.2018"


def test_etrn_driver_uses_structured_fio_fields():
    """СвВодит/ФИО takes last/first/middle name; one-line full_name stays for other docs."""
    from review_processor.repository import ReviewRepository

    root = ET.fromstring(
        _build(
            driver_name="Игнорируем Строку Полностью",
            driver_fields={
                "last_name": "Петров",
                "first_name": "Пётр",
                "middle_name": "Петрович",
                "full_name": "Петров Пётр Петрович",
            },
        )
    )
    fio = root.find("Документ/СодИнфГО/СвВодит/ФИО")
    assert fio is not None
    assert fio.attrib.get("Фамилия") == "Петров"
    assert fio.attrib.get("Имя") == "Пётр"
    assert fio.attrib.get("Отчество") == "Петрович"

    # Compose one-line for TTN / заявка / selects.
    assert (
        ReviewRepository.compose_driver_full_name(
            last_name="Петров", first_name="Пётр", middle_name="Петрович"
        )
        == "Петров Пётр Петрович"
    )
    # Legacy one-line still splits when structured fields are empty.
    legacy = ReviewRepository._normalize_driver_fio_fields(full_name="Сидоров Сидор Сидорович")
    assert legacy["last_name"] == "Сидоров"
    assert legacy["first_name"] == "Сидор"
    assert legacy["middle_name"] == "Сидорович"
    assert legacy["full_name"] == "Сидоров Сидор Сидорович"


def test_etrn_vehicle_uses_structured_catalog_fields():
    """СвТС takes structured driver vehicle fields (ПарТС / ТипВлад)."""
    root = ET.fromstring(
        _build(
            vehicle_line="ignored free text",
            vehicle_json={"vehicle_model": "OLD", "vehicle_number": "A000AA00"},
            vehicle_fields={
                "model": "MAN",
                "number": "В849ВО37",
                "type": "седельный тягач",
                "ownership": "3",
                "capacity_t": "18.5",
                "volume_m3": "86",
                "line": "MAN В849ВО37",
            },
        )
    )
    ts = root.find("Документ/СодИнфГО/СвТС/ТС")
    assert ts is not None
    assert ts.attrib.get("РегНомер") == "В849ВО37"
    assert ts.attrib.get("ТипВлад") == "3"
    part = ts.find("ПарТС")
    assert part is not None
    assert part.attrib.get("Марка") == "MAN"
    assert part.attrib.get("Тип") == "седельный тягач"
    assert part.attrib.get("Грузопод") == "18.5"
    assert part.attrib.get("Вместим") == "86"


def test_etrn_loader_from_production_head():
    """СвЛицПогрГр/РабЛицПогрГр ← производство.Начальник, должность фиксирована."""
    root = ET.fromstring(_build(loader_name="Сидоров Сидор Сидорович"))
    rab = root.find("Документ/СодИнфГО/СвПогруз/СвЛицПогрГр/РабЛицПогрГр")
    assert rab is not None
    assert rab.attrib.get("Должность") == "начальник производства"
    od = rab.find("ОДолжОб")
    assert od is not None
    assert (od.text or "").strip() == "Должностные обязанности"
    fio = rab.find("ФИО")
    assert fio is not None
    assert fio.attrib.get("Фамилия") == "Сидоров"
    assert fio.attrib.get("Имя") == "Сидор"
    assert fio.attrib.get("Отчество") == "Сидорович"
    # Without head_name — no worker block (ИдентРекГО still present).
    root2 = ET.fromstring(_build(loader_name=""))
    assert root2.find("Документ/СодИнфГО/СвПогруз/СвЛицПогрГр/РабЛицПогрГр") is None
    assert root2.find("Документ/СодИнфГО/СвПогруз/СвЛицПогрГр/ИдентРекГО") is not None


def test_compose_vehicle_line_and_normalize():
    from review_processor.repository import ReviewRepository

    assert ReviewRepository.compose_vehicle_line(model="MAN", number="В849ВО37") == "MAN В849ВО37"
    legacy = ReviewRepository._normalize_vehicle("MAN В849ВО37")
    assert legacy is not None
    assert legacy["model"] == "MAN"
    assert legacy["number"] == "В849ВО37"
    assert legacy["ownership"] == "1"
    assert legacy["capacity_t"] == "20"
    structured = ReviewRepository._normalize_vehicle(
        {
            "model": "GAZelle",
            "number": "A123BC77",
            "type": "грузовой автомобиль",
            "ownership": "4",
            "capacity_t": "1.5",
            "volume_m3": "12.5",
        }
    )
    assert structured is not None
    assert structured["line"] == "GAZelle A123BC77"
    assert structured["ownership"] == "4"
    assert structured["capacity_t"] == "1.5"
    assert ReviewRepository._normalize_vehicles_list(["", None, "X"])[0]["model"] == "X"


def test_document_compose_helpers_fill_empty_legacy_strings():
    """TTN / заявка / PoA must get one-line strings even if legacy columns are empty."""
    from review_processor.repository import ReviewRepository

    docs = ReviewRepository.driver_documents_line(
        {
            "documents": "",
            "doc_vu_series": "9900",
            "doc_vu_number": "123456",
            "doc_vu_issuer": "ГИБДД",
            "doc_vu_date": "01.02.2018",
            "doc_inn_fl": "",
        }
    )
    assert "ВУ 99 00 123456" in docs
    assert "кем выд. ГИБДД" in docs
    assert "выд. 01.02.2018" in docs

    carrier = ReviewRepository.carrier_line(
        {
            "carrier": "",
            "carrier_name": 'ООО "Перевозчик"',
            "carrier_inn": "5001002003",
            "carrier_kpp": "500101001",
        }
    )
    assert 'ООО "Перевозчик"' in carrier
    assert "ИНН 5001002003" in carrier

    veh = ReviewRepository.compose_vehicle_line(
        {"model": "MAN", "number": "В849ВО37", "line": ""}
    )
    assert veh == "MAN В849ВО37"

    addr = ReviewRepository.warehouse_address_line(
        {
            "address": "",
            "addr_index": "143420",
            "addr_city": "Истра",
            "addr_street": "ул. Складская",
            "addr_house": "5",
        }
    )
    assert addr == "143420, г. Истра, ул. Складская, д. 5"


def test_etrn_driver_uses_structured_inn_fl():
    root = ET.fromstring(
        _build(
            driver_documents="",
            driver_fields={"doc_inn_fl": "500100200300"},
        )
    )
    vod = root.find("Документ/СодИнфГО/СвВодит")
    assert vod is not None
    assert vod.attrib.get("ИННФЛ") == "500100200300"


def test_compose_driver_documents_line():
    from review_processor.repository import ReviewRepository

    line = ReviewRepository.compose_driver_documents_line(
        {
            "doc_vu_series": "9900",
            "doc_vu_number": "123456",
            "doc_vu_issuer": "ГИБДД г. Москвы",
            "doc_vu_date": "01.02.2018",
            "doc_inn_fl": "500100200300",
        }
    )
    assert "ВУ 99 00 123456" in line
    assert "кем выд. ГИБДД г. Москвы" in line
    assert "выд. 01.02.2018" in line
    assert line.index("кем выд.") < line.index("выд. 01.02.2018")
    assert "ИНН 500100200300" in line
    assert (
        ReviewRepository.driver_documents_line(
            {"documents": "старые документы", "doc_vu_series": ""}
        )
        == "старые документы"
    )
    # Issuer/date alone must not wipe legacy documents text.
    assert (
        ReviewRepository.driver_documents_line(
            {
                "documents": "паспорт 1234 567890",
                "doc_vu_issuer": "ГИБДД",
                "doc_vu_date": "01.02.2018",
            }
        )
        == "паспорт 1234 567890"
    )
    assert ReviewRepository.compose_driver_documents_line(
        {"doc_vu_issuer": "ГИБДД", "doc_vu_date": "01.02.2018"}
    ) == ""
    # Issuer is catalog-only — not part of eTrN СвВодит attrs.
    root = ET.fromstring(
        _build(
            driver_fields={
                "doc_vu_series": "9900",
                "doc_vu_number": "123456",
                "doc_vu_issuer": "ГИБДД г. Москвы",
                "doc_vu_date": "01.02.2018",
            }
        )
    )
    vod = root.find("Документ/СодИнфГО/СвВодит")
    assert vod is not None
    assert vod.attrib.get("СерВУ") == "9900"
    assert vod.attrib.get("ДатаВыдВУ") == "01.02.2018"
    assert "кем" not in ET.tostring(vod, encoding="unicode").lower()
    assert "гибдд" not in ET.tostring(vod, encoding="unicode").lower()


def test_etrn_contacts_use_legal_entity_phone_everywhere():
    """Without carrier_phone, юр.лица phone fills ГО, ГП, перевозчик, водитель, переадресовка."""
    root = ET.fromstring(_build())
    sod = root.find("Документ/СодИнфГО")
    assert sod is not None
    expected = "+79991112233"
    go = sod.find("СвГО/РекИдентГО/Контакт/Тлф")
    gp = sod.find("СвГП/РекИдентГП/Контакт/Тлф")
    carrier = sod.find("СвПер/Контакт/Тлф")
    driver = sod.find("СвВодит/Тлф")
    redirect = sod.find("УказГО/СвПА/КонтПА/Тлф")
    assert go is not None and go.text == expected
    assert gp is not None and gp.text == expected
    assert carrier is not None and carrier.text == expected
    assert driver is not None and driver.text == expected
    assert redirect is not None and redirect.text == expected
    # Must not fall back to driver_phone when le.phone is set.
    for phone in sod.findall(".//Тлф"):
        assert phone.text != "+79001234567"


def test_etrn_carrier_phone_prefers_catalog_over_legal_entity():
    """СвПер/Контакт/Тлф ← телефон перевозчика; ГО/ГП остаются с юр.лица."""
    root = ET.fromstring(
        _build(
            carrier_fields={
                "carrier_name": 'ООО "Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_kpp": "500101001",
                "carrier_phone": "+79007776655",
            },
        )
    )
    sod = root.find("Документ/СодИнфГО")
    assert sod is not None
    carrier = sod.find("СвПер/Контакт/Тлф")
    assert carrier is not None and carrier.text == "+79007776655"
    go = sod.find("СвГО/РекИдентГО/Контакт/Тлф")
    assert go is not None and go.text == "+79991112233"


def test_etrn_ozon_fns_id_fixed_in_id_file():
    """ИдФайл/E — зафиксированный FNSId Озона."""
    assert OZON_CONSIGNEE_EDO_GUID == "2BM-7704217370-774301001-201407110916237240124"
    root = ET.fromstring(_build())
    id_file = root.attrib["ИдФайл"]
    assert id_file.startswith(f"ON_TRNACLGROT__{OZON_CONSIGNEE_EDO_GUID}_")


def test_etrn_carrier_fns_id_in_id_file():
    """ИдФайл/A ← carrier_fns_id из каталога Водители → Перевозчик."""
    fns = "2BM-5001002003-500101001-201501010000000000001"
    root = ET.fromstring(
        _build(
            carrier_fields={
                "carrier_name": 'ООО "Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_kpp": "500101001",
                "carrier_fns_id": fns,
            },
        )
    )
    assert root.attrib["ИдФайл"].startswith(f"ON_TRNACLGROT_{fns}_{OZON_CONSIGNEE_EDO_GUID}_")


def test_etrn_driver_phone_prefers_catalog_over_legal_entity():
    """СвВодит/Тлф ← телефон водителя; остальные контакты остаются с юр.лица."""
    root = ET.fromstring(
        _build(
            driver_fields={"phone": "+79005554433"},
        )
    )
    sod = root.find("Документ/СодИнфГО")
    assert sod is not None
    driver = sod.find("СвВодит/Тлф")
    assert driver is not None and driver.text == "+79005554433"
    go = sod.find("СвГО/РекИдентГО/Контакт/Тлф")
    assert go is not None and go.text == "+79991112233"
    # Empty driver phone → fallback to юр.лица.
    root2 = ET.fromstring(_build(driver_fields={"phone": ""}))
    assert root2.find("Документ/СодИнфГО/СвВодит/Тлф").text == "+79991112233"


def test_etrn_contacts_fallback_to_driver_phone_when_le_phone_empty():
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "",
            },
            driver_phone="+79001234567",
        )
    )
    go = root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Контакт/Тлф")
    assert go is not None
    assert go.text == "+79001234567"


def test_etrn_load_address_uses_structured_fields():
    """СвПогруз takes structured production fields without free-text parse."""
    root = ET.fromstring(
        _build(
            load_address="ignored free text that would parse differently",
            load_addr_fields={
                "Индекс": "141580",
                "КодРегион": "50",
                "Район": "Солнечногорский р-н",
                "Город": "",
                "НаселПункт": "Хоругвино",
                "Улица": "ул. Заводская",
                "Дом": "32/2",
                "Корпус": "",
                "Кварт": "",
                "raw": "141580, Солнечногорский р-н, Хоругвино",
            },
        )
    )
    adr = root.find("Документ/СодИнфГО/СвПогруз/ФАдресПогр/АдресРФ")
    assert adr is not None
    assert adr.attrib.get("Индекс") == "141580"
    assert adr.attrib.get("КодРегион") == "50"
    assert adr.attrib.get("Район") == "Солнечногорский р-н"
    assert adr.attrib.get("НаселПункт") == "Хоругвино"
    assert adr.attrib.get("Улица") == "ул. Заводская"
    assert adr.attrib.get("Дом") == "32/2"


def test_etrn_delivery_address_uses_structured_warehouse_fields():
    """АдресДостГр takes structured warehouse fields without free-text parse."""
    root = ET.fromstring(
        _build(
            delivery_address="ignored free text that would parse differently",
            delivery_addr_fields={
                "Индекс": "143420",
                "КодРегион": "50",
                "Город": "Истра",
                "Улица": "ул. Складская",
                "Дом": "5",
                "Корпус": "1",
                "raw": "143420, г. Истра, ул. Складская, д. 5",
            },
        )
    )
    adr = root.find("Документ/СодИнфГО/СвГП/АдресДостГр/АдресРФ")
    assert adr is not None
    assert adr.attrib.get("Индекс") == "143420"
    assert adr.attrib.get("КодРегион") == "50"
    assert adr.attrib.get("Город") == "Истра"
    assert adr.attrib.get("Улица") == "ул. Складская"
    assert adr.attrib.get("Дом") == "5"
    assert adr.attrib.get("Корпус") == "1"


def test_warehouse_address_line_assembles_for_documents():
    from review_processor.repository import ReviewRepository

    line = ReviewRepository.warehouse_address_line(
        {
            "addr_index": "143420",
            "addr_city": "Истра",
            "addr_street": "ул. Складская",
            "addr_house": "5",
            "address": "legacy",
        }
    )
    assert line == "143420, г. Истра, ул. Складская, д. 5"
    assert (
        ReviewRepository.warehouse_address_line(
            {"address": "старый адрес одной строкой", "addr_city": ""}
        )
        == "старый адрес одной строкой"
    )


def test_production_address_line_assembles_for_documents():
    """TTN / PoA / заявки still get one assembled address string."""
    from review_processor.repository import ReviewRepository

    line = ReviewRepository.compose_production_address_line(
        {
            "addr_index": "141580",
            "addr_district": "Солнечногорский р-н",
            "addr_settlement": "деревня Хоругвино",
            "addr_street": "ул. Заводская",
            "addr_house": "10",
        }
    )
    assert line == "141580, Солнечногорский р-н, деревня Хоругвино, ул. Заводская, д. 10"
    # Legacy one-line still returned when structured fields are empty.
    assert (
        ReviewRepository.production_address_line(
            {"address": "старый адрес одной строкой", "addr_city": ""}
        )
        == "старый адрес одной строкой"
    )


def test_etrn_ryazan_index_is_not_kaliningrad():
    """390xxx is Ryazan (62); index[:2]==39 must not become Kaliningrad."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567",
                "address": "390528, Рязанская область, с. Алеканово, ул. Полевая, д.62г",
                "signatories": "Иванов Иван Иванович",
            }
        )
    )
    adr = root.find("Документ/СодИнфГО/СвГО/РекИдентГО/Адрес/АдрРФ")
    assert adr is not None
    assert adr.attrib.get("КодРегион") == "62"
    assert adr.attrib.get("Индекс") == "390528"
    assert "Полевая" in adr.attrib.get("Улица", "")
    assert adr.attrib.get("НаселПункт") == "Алеканово"
