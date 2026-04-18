from config.entities import (
    get_all_entities,
    get_entity_by_npi,
    get_entity_by_program,
)
from config.models import Program


def test_confirmed_entity_source_of_truth():
    by_key = {entity.key: entity for entity in get_all_entities()}

    assert by_key["MARYS_HOME"].billing_npi == "1437871753"
    assert by_key["MARYS_HOME"].tax_id == "861567663"

    assert by_key["NHCS"].billing_npi == "1700297447"
    assert by_key["NHCS"].tax_id == "465232420"

    assert by_key["KJLN"].billing_npi == "1306491592"
    assert by_key["KJLN"].tax_id == "821966562"


def test_entity_lookup_by_program_and_npi():
    assert get_entity_by_program(Program.MARYS_HOME).key == "MARYS_HOME"
    assert get_entity_by_program(Program.NHCS).key == "NHCS"
    assert get_entity_by_program(Program.KJLN).key == "KJLN"

    assert get_entity_by_npi("1437871753").program == Program.MARYS_HOME
    assert get_entity_by_npi("1700297447").program == Program.NHCS
    assert get_entity_by_npi("1306491592").program == Program.KJLN


def test_stale_npis_are_not_known_entities():
    assert get_entity_by_npi("1588094513") is None
    assert get_entity_by_npi("1235723785") is None
