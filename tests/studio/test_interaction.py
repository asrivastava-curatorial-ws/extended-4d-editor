"""The interaction window: an interactive show's blocks as boxes and wires."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox, QLineEdit

from fourdvr_play.config import EXAMPLE, PlayConfigError
from fourdvr_studio.gui import theme
from fourdvr_studio.gui.interaction import InteractionWindow, parse_levels, tidy


@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance() or QApplication([])
    theme.apply(instance)       # type: ignore[arg-type]
    return instance             # type: ignore[return-value]


@pytest.fixture
def win(app: QApplication):  # type: ignore[no-untyped-def]
    window = InteractionWindow()
    window.ask_discard = lambda: "discard"
    window.show()
    app.processEvents()
    yield window
    window.dirty = False
    window.close()
    theme.dispose(window)


def at(win: InteractionWindow, scene_point: QPointF) -> QPoint:
    return win.view.mapFromScene(scene_point)


def test_blocks_are_added_set_up_and_connected_from_the_panel(win: InteractionWindow, app: QApplication) -> None:
    assert win.windowTitle() == "Interaction - not saved yet" and win.notes.item(0).text().startswith("Add a 'When' block")
    kinds = [win.kinds.item(k).text().strip() for k in range(win.kinds.count())]
    assert kinds[:3] == ["WHEN", "When an event arrives", "When a button is pressed"] and "Ask" in kinds and len(kinds) == 23
    win.kinds.itemActivated.emit(win.kinds.item(1))                 # double-click / Enter on "When an event arrives"
    hit = win.graph.blocks["event1"]
    assert win.selected_blocks() == ["event1"] and win.dirty and hit.pos is not None
    jolt = win.add_block("react", (300.0, 0.0))
    points = win.add_block("score", (300.0, 120.0))
    assert (jolt.id, points.id) == ("react1", "score1") and len(win.scene.items_by_id) == 3
    # the panel shows the chosen block's settings; editing a field changes the block and keeps the field
    win.select("react1")
    app.processEvents()
    fields = win.props.findChildren(QDoubleSpinBox)
    assert len(fields) == 8 and [f.accessibleName() for f in fields[:3]] == [f"push: forward, left, up, value {k}" for k in (1, 2, 3)]
    fields[2].setValue(2.5)
    assert win.graph.blocks["react1"].params["accel"] == (0.0, 0.0, 2.5) and fields[2] in win.props.findChildren(QDoubleSpinBox)
    effects = next(e for e in win.props.findChildren(QLineEdit) if e.accessibleName() == "effects (name = level 0-1)")
    effects.setText("vibration = 1, air=0.5")
    effects.editingFinished.emit()
    assert win.graph.blocks["react1"].params["effects"] == {"vibration": 1.0, "air": 0.5}
    effects.setText("vibration = loud")
    effects.editingFinished.emit()
    assert "write name = level" in win.status.text() and win.graph.blocks["react1"].params["effects"] == {"vibration": 1.0, "air": 0.5}
    # "then": the combo box of the event block connects it
    win.select("event1")
    app.processEvents()
    combo = next(c for c in win.props.findChildren(QComboBox) if c.accessibleName() == "Connect then to a block")
    assert [combo.itemData(k) for k in range(combo.count())] == ["", "react1", "score1"]
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    assert win.graph.blocks["event1"].then == {"then": ["react1"]} and len(win.scene.wires) == 1
    assert win.connect_blocks("event1", "then", "score1") and not win.connect_blocks("event1", "then", "score1")
    assert "already connected" in win.status.text()
    assert not win.connect_blocks("react1", "then", "event1") and "nothing can lead to a 'when' block" in win.status.text()
    assert win.words.toPlainText().splitlines() == [
        "when an event arrives: event “hit”", "   -> move and effects: push + vibration, air: seat", "   -> give points: +10 points: seat"]
    # a name is how the file refers to a block
    name = next(e for e in win.props.findChildren(QLineEdit) if e.accessibleName() == "Name of the block")
    name.setText("hit")
    name.editingFinished.emit()
    name.editingFinished.emit()                                     # Qt says "finished" twice: no complaint the second time
    assert list(win.graph.blocks) == ["hit", "react1", "score1"] and win.selected_blocks() == ["hit"]
    assert "exists already" not in win.status.text()
    assert not win.rename("react1", "hit") and "exists already" in win.status.text()
    assert not win.rename("react1", "two words") and "not a name for a block" in win.status.text()


def test_a_wire_is_pulled_with_the_mouse_and_removed_with_del(win: InteractionWindow, app: QApplication) -> None:
    win.add_block("on_input", (0.0, 0.0))
    win.add_block("cooldown", (300.0, 0.0))
    win.add_block("send", (600.0, 120.0))
    win.view.fit()
    app.processEvents()
    port = win.scene.items_by_id["input1"].output_point("then")
    target = win.scene.items_by_id["cooldown1"].input_point() + QPointF(60.0, 0.0)
    view = win.view.viewport()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, port))
    QTest.mouseMove(view, at(win, (port + target) / 2.0))
    assert win.scene._rubber is not None                             # the dashed wire follows the pointer
    QTest.mouseMove(view, at(win, target))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, target))
    assert win.graph.blocks["input1"].then == {"then": ["cooldown1"]} and win.scene._rubber is None
    # the 'else' dot of the cooldown block is its second one
    other = win.scene.items_by_id["cooldown1"].output_point("else")
    end = win.scene.items_by_id["send1"].input_point() + QPointF(40.0, 4.0)
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, other))
    QTest.mouseMove(view, at(win, end))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, end))
    assert win.graph.blocks["cooldown1"].then == {"else": ["send1"]}
    # released over nothing: no wire
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, port))
    QTest.mouseMove(view, at(win, QPointF(150.0, 400.0)))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, QPointF(150.0, 400.0)))
    assert len(win.scene.wires) == 2
    # dragging a block moves it, and that can be undone
    before = win.graph.blocks["send1"].pos
    grip = win.scene.items_by_id["send1"].mapToScene(QPointF(100.0, 12.0))
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, grip))
    QTest.mouseMove(view, at(win, grip + QPointF(40.0, 30.0)))
    QTest.mouseMove(view, at(win, grip + QPointF(80.0, 60.0)))
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, at(win, grip + QPointF(80.0, 60.0)))
    moved = win.graph.blocks["send1"].pos
    assert moved is not None and before is not None and moved[0] > before[0] + 40.0 and moved[1] > before[1] + 30.0
    win.undo()
    assert win.graph.blocks["send1"].pos == before
    win.redo()
    assert win.graph.blocks["send1"].pos == moved
    # a wire is chosen and removed with Del; so is a block, with its wires
    win.scene.clearSelection()
    win.scene.wires[0].setSelected(True)
    win.view.setFocus()
    QTest.keyClick(win.view, Qt.Key.Key_Delete)
    assert len(win.scene.wires) == 1 and "removed 1 wire(s)" in win.status.text()
    win.select("cooldown1")
    win.delete_selection()
    assert list(win.graph.blocks) == ["input1", "send1"] and win.scene.wires == []
    for _ in range(2):
        win.undo()
    assert len(win.scene.wires) == 2 and list(win.graph.blocks) == ["input1", "cooldown1", "send1"]


def test_a_play_file_is_opened_edited_and_saved_without_touching_the_rest(win: InteractionWindow, tmp_path: Path) -> None:
    config = tmp_path / "play.toml"
    config.write_text(EXAMPLE.replace('controller = "controller.toml"', "# no controller").replace('platform = "PLATFORM-01"', "")
                      .replace('platform = "PLATFORM-02"', ""), encoding="utf-8")
    win.open_path(config)
    assert len(win.graph.blocks) == 10 and not win.dirty and win.seats == ["A1", "A2"] and win.teams == ["blue", "red"]
    assert all(b.pos is not None for b in win.graph.blocks.values()) and len(win.scene.wires) == 8
    assert win.graph.blocks["hit"].pos == (0.0, 0.0) and win.graph.blocks["hit-jolt"].pos[0] == 260.0      # tidied: columns
    assert win.graph.blocks["fire-send"].pos == (520.0, win.graph.blocks["fire-rate"].pos[1])              # a chain in a row
    assert win.windowTitle() == "Interaction - play.toml"
    win.select("route")
    assert win.scene.items_by_id["route"].height > win.scene.items_by_id["hit"].height                    # five outputs
    assert win.set_param("route", "seconds", 12.0) and not win.set_param("route", "seconds", 500.0)
    assert "seconds must be between 2 and 120" in win.status.text()
    assert win.set_param("route", "choices", ["Through the canyon", "Over the ridge", "Wait"])
    bonus = win.add_block("set_state", (520.0, 400.0))
    assert win.connect_blocks("route", "c", bonus.id) and win.windowTitle().endswith("play.toml *")
    assert not win.save() and win.status.text() == "not saved: block set_state1 (set a shared value): key is missing"
    assert win.selected_blocks() == ["set_state1"] and win.notes.item(0).text().startswith("! block set_state1")
    assert win.set_param("set_state1", "key", "route") and win.set_param("set_state1", "value", "wait")
    assert win.save() and not win.dirty and "saved play.toml" in win.status.text()
    text = config.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert "# overall strength, 0..2 (start low)" in text and data["play"]["title"] == "Asteroid run" and len(data["play"]["seats"]) == 2
    route = next(b for b in data["play"]["blocks"] if b["id"] == "route")
    assert route["seconds"] == 12.0 and route["then"]["c"] == ["set_state1"] and route["pos"] == [260, 318]
    again = InteractionWindow()
    try:
        again.open_path(config)                                      # the positions were written: nothing is tidied away
        assert again.graph.to_list() == win.graph.to_list() and again.graph.blocks["set_state1"].pos == (520.0, 400.0)
    finally:
        theme.dispose(again)
    # a new file gets the least a play.toml needs; a block that needs a film says so after saving
    win.new()
    win.add_block("on_time", (0.0, 0.0))
    win.set_param("time1", "clock", "film")
    fresh = tmp_path / "new" / "show.toml"
    win.ask_save_name = lambda: str(fresh)
    assert not win.save() and "not saved" in win.status.text() and not fresh.exists()                     # no such folder
    fresh.parent.mkdir()
    assert win.save() and fresh.read_text(encoding="utf-8").startswith('[play]\ntitle = "Interactive show"\n\n[[play.blocks]]')
    assert "this show cannot run yet: block time1 follows or steers a film" in win.status.text()
    broken = tmp_path / "broken.toml"
    broken.write_text('[[play.blocks]]\nid = "x"\ntype = "nonsense"\n', encoding="utf-8")
    with pytest.raises(PlayConfigError, match="unknown type 'nonsense'"):
        win.open_path(broken)
    assert win.path == str(fresh)                                    # what was open stays open


def test_tidy_levels_and_duplicates(win: InteractionWindow) -> None:
    for kind in ("on_phase", "delay", "send", "react"):
        win.add_block(kind, (0.0, 0.0))
    win.connect_blocks("phase1", "then", "delay1")
    win.connect_blocks("delay1", "then", "send1")
    win.connect_blocks("send1", "then", "delay1")                    # a rhythm: allowed, because a delay is in the loop
    assert not win.connect_blocks("send1", "then", "send1")
    tidy(win.graph)
    assert [win.graph.blocks[k].pos for k in ("phase1", "delay1", "send1", "react1")] == [(0.0, 0.0), (260.0, 0.0), (520.0, 0.0), (780.0, 104.0)]
    win.refresh()
    win.select("react1")
    win.set_param("react1", "accel", [1.0, 0.0, 0.0])
    win.duplicate_selection()
    copy = win.graph.blocks["react2"]
    assert copy.params["accel"] == (1.0, 0.0, 0.0) and copy.pos == (810.0, 134.0) and win.selected_blocks() == ["react2"]
    assert parse_levels("fog") == {"fog": 1.0} and parse_levels(" wind = 0.5 ; air=1,") == {"wind": 0.5, "air": 1.0}
    notes = [win.notes.item(k).text() for k in range(win.notes.count())]
    assert "block react1 (move and effects): nothing leads to it" in notes and "! block send1 (tell the game): event is missing" in notes
    win.notes.itemClicked.emit(win.notes.item(0))
    assert win.selected_blocks() == ["send1"]
