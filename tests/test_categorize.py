from decimal import Decimal as D

import pytest

from receiptbot.categorize import (
    Categorizer,
    CategoryGuess,
    load_seed_rules,
    match_category,
    normalize_key,
    shorten,
)
from receiptbot.ledger import Ledger
from receiptbot.models import Category, Item

CATS = [
    Category("c-pets", "Pets", "Everyday"),
    Category("c-house", "Household", "Everyday"),
    Category("c-groc", "Groceries", "Everyday"),
    Category("c-gifts", "\U0001f381 Gifts", "Fun"),
    Category("c-home", "Home Maintenance", "Everyday"),
]

DOG_FOOD = "Good Boy Kibble Adult Dry Dog Food, Chicken & Rice Formula, 30 lb. Bag"
TRASH_BAGS = "Sturdy Tall Kitchen Drawstring Trash Bags, 13 Gallon, 90 Count (Pack of 1)"


class FakeLlm:
    def __init__(self, guesses):
        self.guesses = guesses
        self.calls = []

    def categorize_items(self, descriptions, category_names):
        self.calls.append((descriptions, category_names))
        return self.guesses


@pytest.fixture
def ledger():
    return Ledger(":memory:")


def make(ledger, **kwargs):
    return Categorizer(ledger, lambda: CATS, **kwargs)


def item(title, total="1.00"):
    return Item(description=title, line_total=D(total))


def test_shorten_and_key_drop_sizes_and_keep_product_words():
    assert shorten(DOG_FOOD) == "adult dry dog food"
    assert shorten(TRASH_BAGS) == "kitchen drawstring trash bags"
    assert normalize_key(TRASH_BAGS) == "kitchen drawstring trash bag"
    assert shorten("Paws & Co Rope Tug Dog Toy for Large Dogs") == "rope tug dog toy"
    assert shorten("USB-C Cable") == "usb c cable"
    assert shorten("") == ""


def test_key_is_stable_across_size_variants():
    a = normalize_key("Sturdy Tall Kitchen Drawstring Trash Bags, 13 Gallon, 90 Count")
    b = normalize_key("Sturdy Tall Kitchen Drawstring Trash Bags 13 Gallon 45 Count (Pack of 2)")
    assert a == b


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("Pets", "c-pets"),
        ("pets!", "c-pets"),
        ("gifts", "c-gifts"),
        ("house", "c-house"),
        ("Grocery", "c-groc"),
        ("maintenance", "c-home"),
        ("ho", None),  # ambiguous prefix: Household or Home Maintenance
        ("vacation", None),
        ("", None),
    ],
)
def test_match_category(answer, expected):
    found = match_category(answer, CATS)
    assert (found.id if found else None) == expected


def test_unknown_item_without_rules_or_model_needs_a_person(ledger):
    [result] = make(ledger).assign([item(DOG_FOOD)])
    assert result.category_id is None
    assert result.guesses == []


def test_exact_rule_hit_and_hit_count(ledger):
    cat = make(ledger)
    cat.learn(normalize_key(DOG_FOOD), "c-pets", "human")
    [result] = cat.assign([item(DOG_FOOD)])
    assert (result.category_id, result.category_source) == ("c-pets", "rule")
    assert ledger.get_rule(normalize_key(DOG_FOOD)).hit_count == 1


def test_token_overlap_match_is_conservative(ledger):
    cat = make(ledger)
    cat.learn("kitchen drawstring trash bag", "c-house", "human")
    [near] = cat.assign([item("Drawstring Trash Bags, 13 Gallon")])
    assert near.category_id == "c-house"  # 3 of 4 distinct words agree
    [close] = cat.assign([item("Mighty Tall Kitchen Trash Bags, 13 Gallon")])
    assert close.category_id is None  # 3 of 5: offered as a guess, not assigned
    assert close.guesses == ["c-house"]
    [far] = cat.assign([item("Kitchen Knife Set")])
    assert far.category_id is None


def test_weak_overlap_becomes_a_guess_not_an_assignment(ledger):
    cat = make(ledger)
    cat.learn("dry dog food", "c-pets", "human")
    [result] = cat.assign([item("Wet Dog Food Variety")])
    assert result.category_id is None
    assert result.guesses == ["c-pets"]


def test_seed_rules(ledger, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("dog food: Pets\ntrash bags: household\nmystery: No Such Category\n")
    seeds = load_seed_rules(path, CATS)
    assert len(seeds) == 2
    results = make(ledger, seed_rules=seeds).assign([item(DOG_FOOD), item(TRASH_BAGS)])
    assert [r.category_id for r in results] == ["c-pets", "c-house"]
    assert all(r.category_source == "rule" for r in results)


def test_learned_rule_beats_seed_rule(ledger, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("dog food: Pets\n")
    cat = make(ledger, seed_rules=load_seed_rules(path, CATS))
    cat.learn(normalize_key(DOG_FOOD), "c-gifts", "human")
    assert cat.assign([item(DOG_FOOD)])[0].category_id == "c-gifts"


def test_rule_pointing_at_removed_category_is_ignored(ledger):
    cat = make(ledger)
    cat.learn(normalize_key(DOG_FOOD), "c-deleted", "human")
    assert cat.assign([item(DOG_FOOD)])[0].category_id is None


def test_model_only_sees_unknown_items_and_category_names(ledger):
    llm = FakeLlm([CategoryGuess("Household", 0.95)])
    cat = make(ledger, llm=llm)
    cat.learn(normalize_key(DOG_FOOD), "c-pets", "human")
    results = cat.assign([item(DOG_FOOD), item(TRASH_BAGS)])
    assert llm.calls == [([TRASH_BAGS], [c.name for c in CATS])]
    assert [(r.category_id, r.category_source) for r in results] == [
        ("c-pets", "rule"),
        ("c-house", "llm"),
    ]
    # A model guess is not a rule until a person approves the transaction.
    assert ledger.get_rule(normalize_key(TRASH_BAGS)) is None


def test_no_model_call_when_every_item_has_a_rule(ledger):
    llm = FakeLlm([])
    cat = make(ledger, llm=llm)
    cat.learn(normalize_key(DOG_FOOD), "c-pets", "human")
    cat.assign([item(DOG_FOOD)])
    assert llm.calls == []


def test_low_confidence_or_invented_category_becomes_a_question(ledger):
    llm = FakeLlm(
        [
            CategoryGuess("Household", 0.5, alternative="Groceries"),
            CategoryGuess("Made Up Category", 0.99),
            CategoryGuess(None, 0.0),
        ]
    )
    results = make(ledger, llm=llm).assign([item("a"), item("b"), item("c")])
    assert [r.category_id for r in results] == [None, None, None]
    assert results[0].guesses == ["c-house", "c-groc"]
    assert results[1].guesses == []


def test_model_failure_falls_back_to_asking(ledger):
    class Broken:
        def categorize_items(self, descriptions, category_names):
            raise RuntimeError("boom")

    assert make(ledger, llm=Broken()).assign([item("thing")])[0].category_id is None


def test_changed_category_replaces_rule(ledger):
    cat = make(ledger)
    cat.learn("dog toy", "c-pets", "human")
    cat.learn("dog toy", "c-gifts", "approved")
    rule = ledger.get_rule("dog toy")
    assert (rule.category_id, rule.origin) == ("c-gifts", "approved")
