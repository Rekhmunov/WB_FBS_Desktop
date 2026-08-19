"""Schema-oriented checks for Ozon ЭЗЗ (заказ-заявка) title-1 XML draft."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

from review_processor.ozon_zakaz import OZON_FNS_ID, build_ozon_zakaz_xml


def _build(**overrides):
    kwargs = dict(
        item={
            "supply_order_id": 120035796,
            "supply_order_number": "2000061286750",
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
            "address": "101000, г. Москва, ул. Ленина, д. 1",
        },
        vehicle_fields={
            "model": "MAN",
            "number": "В849ВО37",
            "type": "грузовой автомобиль",
            "ownership": "1",
            "capacity_t": "20",
            "volume_m3": "20",
        },
        carrier_fields={
            "carrier_name": 'ООО "Перевозчик"',
            "carrier_inn": "5001002003",
            "carrier_kpp": "500101001",
        },
        cargoes_json={
            "version": 2,
            "groups": [{"type": "BOX", "content_type": "MONO", "count": 41}],
            "transport_cargoes": [
                {"type": "PALLET", "transport_cargo_id": "a", "box_count": 11},
                {"type": "PALLET", "transport_cargo_id": "b", "box_count": 30},
            ],
        },
        load_address="141580, Московская обл., г. Химки, ул. Заводская, д. 10",
        delivery_address="143420, Московская обл., г. Истра, ул. Складская, д. 5",
        now=datetime(2026, 8, 10, 9, 31, 11),
    )
    kwargs.update(overrides)
    return build_ozon_zakaz_xml(**kwargs)


def test_zakaz_xml_core_shape():
    root = ET.fromstring(_build())
    assert root.tag == "Файл"
    assert root.attrib["ВерсФорм"] == "5.01"
    assert root.attrib["ИдФайл"].startswith("ON_ZAKZVGO_")
    assert "DRAFT" not in root.attrib["ИдФайл"]

    doc = root.find("Документ")
    assert doc is not None
    assert doc.attrib["КНД"] == "1110361"
    assert doc.attrib["Функция"] == "Заказ"
    assert "7701234567" in doc.attrib.get("НаимЭкСубСост", "")

    sod = doc.find("СодИнфГО")
    assert sod is not None
    assert sod.attrib["НомЗак"] == "2000061286750"
    assert "Предоставление заказа" in sod.attrib.get("СодОпер", "")

    go = sod.find("СвГО/ИдСв/СвЮЛУч")
    assert go is not None
    assert go.attrib["ИННЮЛ"] == "7701234567"
    go_adr = sod.find("СвГО/Адрес/АдрРФ")
    assert go_adr is not None
    assert len(go_adr.attrib.get("Индекс", "")) == 6
    assert go_adr.attrib.get("КодРегион")
    assert sod.find("СвГО/Конт/Тлф") is not None
    assert sod.find("СвГО/Конт/Тлф").text == "+7 (999) 111-22-33"

    prv = sod.find("СвПрв/ИдСв/СвЮЛУч")
    assert prv is not None
    assert prv.attrib["ИННЮЛ"] == "5001002003"
    assert sod.find("СвПрв/Конт/Тлф").text  # required non-empty

    assert sod.find("ПунктПод") is not None
    pod_adr = sod.find("ПунктПод/АдрПунктПод/Адрес/АдрРФ")
    assert pod_adr is not None
    assert len(pod_adr.attrib.get("Индекс", "")) == 6

    assert sod.find("АдрПункт[@Опер='Погрузка']") is not None
    assert sod.find("АдрПункт[@Опер='Выгрузка']") is not None
    unload = sod.find("АдрПункт[@Опер='Выгрузка']/АдресПункт/Адрес/АдрРФ")
    assert unload is not None
    assert len(unload.attrib.get("Индекс", "")) == 6

    op = sod.find("ОпГруз")
    assert op is not None
    assert op.attrib["КолГрМест"] == "2"
    assert op.attrib["МетОпрМасс"] == "03"
    assert op.attrib["Объем"] == "20.00"
    assert "." in op.find("МасГруз").attrib.get("МасБрутЗнач", "")
    dims = op.find("РазмерГрМест").attrib
    assert dims["ВысЗнач"] == "1.800"
    assert op.find("Пункт").attrib.get("Погр") == "1"
    assert op.find("Пункт").attrib.get("Выгр") == "2"

    ts = sod.find("ПарТСПрвз")
    assert ts is not None
    assert ts.attrib["Тип"] == "грузовой автомобиль"
    assert ts.attrib["Грузопод"] == "20.00"
    assert ts.attrib["Вместим"] == "20.00"

    podp = doc.find("ПодпИнфГО")
    assert podp is not None
    assert podp.attrib.get("СпосПодтПолном") == "1"
    assert podp.find("ФИО").attrib.get("Фамилия") == "Иванов"


def test_zakaz_uses_supply_number_as_nomzak():
    root = ET.fromstring(
        _build(
            item={
                "supply_order_id": 1,
                "supply_order_number": "020-111",
                "supplier_name": "X",
                "warehouse_name": "W",
                "supply_date": "2026-09-01",
            }
        )
    )
    assert root.find("Документ/СодИнфГО").attrib["НомЗак"] == "020-111"


def test_zakaz_fills_index_when_only_warehouse_name():
    """Unload point without structured address must still get Индекс (АдрРФ required)."""
    root = ET.fromstring(
        _build(
            delivery_address="",
            delivery_addr_fields=None,
            item={
                "supply_order_id": 1,
                "supply_order_number": "1",
                "supplier_name": "X",
                "warehouse_name": "ХОРУГВИНО_РФЦ",
                "supply_date": "2026-08-15",
            },
        )
    )
    unload = root.find("Документ/СодИнфГО/АдрПункт[@Опер='Выгрузка']/АдресПункт/Адрес/АдрРФ")
    assert unload is not None
    assert len(unload.attrib.get("Индекс", "")) == 6
    assert unload.attrib.get("КодРегион")


def test_zakaz_phone_never_empty():
    root = ET.fromstring(_build(le={
        "full_name": 'ООО "Тест"',
        "requisites": "ИНН 7701234567 КПП 770101001",
        "signatories": "Иванов Иван Иванович",
        "phone": "",
        "address": "101000, г. Москва, ул. Ленина, д. 1",
    }, driver_phone=""))
    go_phone = root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text
    assert go_phone
    # Contour.Logistics hides +7 (000)… as empty — never emit that stub.
    assert "000" not in (go_phone or "")
    assert root.find("Документ/СодИнфГО/СвПрв/Конт/Тлф").text


def test_zakaz_go_phone_falls_back_to_driver_like_etrn():
    """Пустой le.phone → как в эТрН: телефон водителя, не Contour-скрытый 000."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 8701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
            driver_phone="+79001234567",
            driver_fields={"phone": "+79005554433"},
            carrier_fields={
                "carrier_name": 'ООО "Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_phone": "+79007776655",
            },
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (900) 555-44-33"
    assert root.find("Документ/СодИнфГО/СвПрв/Конт/Тлф").text == "+7 (900) 777-66-55"


def test_zakaz_carrier_phone_prefers_catalog_over_legal_entity():
    """СвПрв/Конт/Тлф ← телефон перевозчика; СвГО остаётся с юр.лица."""
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
    assert root.find("Документ/СодИнфГО/СвПрв/Конт/Тлф").text == "+7 (900) 777-66-55"
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (999) 111-22-33"


def test_zakaz_ozon_fns_id_constant_and_no_unload_owner():
    """Ozon FNSId — общая константа с эТрН; в Заявке нет слота E и нет ОргВладИнфр на выгрузке."""
    assert OZON_FNS_ID == "2BM-7704217370-774301001-201407110916237240124"
    root = ET.fromstring(_build())
    unload = root.find("Документ/СодИнфГО/АдрПункт[@Опер='Выгрузка']")
    assert unload is not None
    assert unload.find("ОргВладИнфр") is None
    load = root.find("Документ/СодИнфГО/АдрПункт[@Опер='Погрузка']/ОргВладИнфр")
    assert load is not None
    assert load.attrib.get("ИННВладИнфр") == "7701234567"


def test_zakaz_carrier_fns_id_in_id_file():
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
    assert root.attrib["ИдФайл"].startswith(f"ON_ZAKZVGO_{fns}_")


def test_zakaz_go_phone_from_legal_entity_field():
    """СвГО/Конт/Тлф ← phone юр.лица, даже если есть телефон водителя."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "+7 (495) 111-22-33",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
            driver_phone="+79001234567",
            driver_fields={"phone": "+79005554433"},
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (495) 111-22-33"
    # Driver phone must not replace shipper phone.
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text != "+7 (900) 123-45-67"


def test_zakaz_go_phone_from_legal_entity_requisites():
    """Если поле phone пустое — берём номер из реквизитов юр.лица (как в эТрН)."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001 тел. +7 495 222-33-44",
                "signatories": "Иванов Иван Иванович",
                "phone": "",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
            driver_phone="+79001234567",
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (495) 222-33-44"


def test_zakaz_go_phone_ten_digit_local():
    """10-значный номер из поля phone юр.лица → +7 (…) в СвГО."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "9991234567",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (999) 123-45-67"


def test_zakaz_go_phone_not_parsed_from_inn():
    """ИНН в реквизитах на «8…» не должен становиться телефоном ГО."""
    from review_processor.ozon_zakaz import _phone_from_requisites, _shipper_phone_from_le

    assert _phone_from_requisites("ИНН 8701234567 КПП 770101001") == ""
    assert _shipper_phone_from_le({
        "phone": "",
        "requisites": "ИНН 8701234567 КПП 770101001",
    }) == ""


def test_zakaz_go_phone_from_shipper_phone_param():
    """Явный shipper_phone (как carrier_phone у перевозчика) важнее пустого le.phone."""
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 8701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
            shipper_phone="+7 916 555-44-33",
            carrier_fields={
                "carrier_name": 'ООО "Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_phone": "+79007776655",
            },
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (916) 555-44-33"
    assert root.find("Документ/СодИнфГО/СвПрв/Конт/Тлф").text == "+7 (900) 777-66-55"


def test_zakaz_go_phone_from_legal_entities_catalog():
    """Если le.phone пуст — берём phone из каталога legal_entities."""
    root = ET.fromstring(
        _build(
            le={
                "id": 1,
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "",
                "address": "101000, г. Москва, ул. Ленина, д. 1",
            },
            legal_entities=[
                {"id": 1, "short_name": "Тест", "phone": ""},
                {"id": 2, "short_name": "Другое", "phone": "+7 (495) 111-00-00"},
            ],
        )
    )
    assert root.find("Документ/СодИнфГО/СвГО/Конт/Тлф").text == "+7 (495) 111-00-00"


def test_zakaz_go_addr_fias_when_set():
    """При заполненном addr_fias юр.лица — СвГО/Адрес/АдрФИАС, не АдрРФ."""
    fias = "ff159a6c-6a6f-442b-9c34-10969dda9bb1"
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест Поставщик"',
                "short_name": "Тест",
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "+79991112233",
                "addr_index": "390528",
                "addr_region_code": "62",
                "addr_district": "муниципальный округ Рязанский",
                "addr_settlement": "Алеканово",
                "addr_street": "Полевая",
                "addr_house": "62г",
                "addr_fias": fias,
            },
        )
    )
    go_fias = root.find("Документ/СодИнфГО/СвГО/Адрес/АдрФИАС")
    assert go_fias is not None
    assert go_fias.attrib.get("ИдНом") == fias
    assert go_fias.findtext("Регион") == "62"
    assert root.find("Документ/СодИнфГО/СвГО/Адрес/АдрРФ") is None


def test_zakaz_carrier_addr_fias_when_set():
    """При carrier_addr_fias — СвПрв/Адрес/АдрФИАС."""
    fias = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    root = ET.fromstring(
        _build(
            carrier_fields={
                "carrier_name": 'ООО "Перевозчик"',
                "carrier_inn": "5001002003",
                "carrier_kpp": "500101001",
                "carrier_phone": "+79007776655",
                "carrier_addr_index": "155312",
                "carrier_addr_region_code": "37",
                "carrier_addr_district": "Вичугский",
                "carrier_addr_settlement": "Чертовищи",
                "carrier_addr_street": "9 мая",
                "carrier_addr_house": "21",
                "carrier_addr_fias": fias,
            },
        )
    )
    prv_fias = root.find("Документ/СодИнфГО/СвПрв/Адрес/АдрФИАС")
    assert prv_fias is not None
    assert prv_fias.attrib.get("ИдНом") == fias
    assert prv_fias.findtext("Регион") == "37"
    assert root.find("Документ/СодИнфГО/СвПрв/Адрес/АдрРФ") is None


def test_zakaz_addr_without_fias_stays_adr_rf():
    """Без ФИАС — как раньше АдрРФ."""
    root = ET.fromstring(_build())
    assert root.find("Документ/СодИнфГО/СвГО/Адрес/АдрРФ") is not None
    assert root.find("Документ/СодИнфГО/СвГО/Адрес/АдрФИАС") is None


def test_zakaz_fias_survives_address_line_fallback():
    """ФИАС не теряется, если структурированных полей нет и адрес разобран из строки."""
    fias = "ff159a6c-6a6f-442b-9c34-10969dda9bb1"
    root = ET.fromstring(
        _build(
            le={
                "full_name": 'ООО "Тест"',
                "requisites": "ИНН 7701234567 КПП 770101001",
                "signatories": "Иванов Иван Иванович",
                "phone": "+79991112233",
                "address": "390528, Рязанская область, с. Алеканово, ул. Полевая, д. 62г",
                "addr_fias": fias,
            },
        )
    )
    go_fias = root.find("Документ/СодИнфГО/СвГО/Адрес/АдрФИАС")
    assert go_fias is not None
    assert go_fias.attrib.get("ИдНом") == fias
    assert go_fias.findtext("Регион")  # region recovered from address/index
