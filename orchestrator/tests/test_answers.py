from sdlc import answers

QS = [
    {"id": "q1", "text": "What limit per client?", "default": "100 req/min", "audience": "po"},
    {"id": "q2", "text": "Response when limited?", "default": "429 + Retry-After", "audience": "tech"},
    {"id": "q3", "text": "Any exempt clients?", "default": "None", "audience": "po"},
]


def test_render_then_parse_round_trip():
    text = answers.render("run-x", 1, QS)
    text = text.replace("Answer:\n", "Answer: 200 per minute for paid plans\n", 1)
    text = text.replace("## q3 · Any exempt clients?  (for: po)\nDefault: None\nAnswer:",
                        "## q3 · Any exempt clients?  (for: po)\nDefault: None\nAnswer: skip")
    result = {a["id"]: a for a in answers.parse(text, QS)}
    assert result["q1"] == {"id": "q1", "value": "200 per minute for paid plans", "source": "human"}
    assert result["q2"]["source"] == "default" and result["q2"]["value"] == "429 + Retry-After"
    assert result["q3"] == {"id": "q3", "value": None, "source": "skipped"}


def test_multiline_answer_is_kept():
    text = "## q1 · x\nDefault: d\nAnswer:\nline one\nline two\n\n## q2 · y\nAnswer:\n"
    result = answers.parse(text, QS[:2])
    assert result[0]["value"] == "line one\nline two"
    assert result[1]["source"] == "default"


def test_missing_question_falls_back_to_default():
    assert answers.parse("", QS)[0]["source"] == "default"
