"""The interaction window: the logic of an interactive show as boxes and wires.

It edits the ``[[play.blocks]]`` of a play.toml (docs/PLAY.md): every box is one block, every wire
one "then". Nothing else of the file is touched when it is saved. What can be done with the mouse
can also be done from the panel on the right, with the keyboard: add a block, set it up, connect it.

    drag a block                move it
    drag from a dot on the right of a block to another block    connect them
    click a wire, Del           remove the wire
    wheel                       zoom;  middle button or Alt + drag: move the view
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QShortcut,
    QTransform,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSceneMouseEvent,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStyleOptionGraphicsItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fourdvr_play.config import PlayConfigError, load_play_config, save_blocks
from fourdvr_play.logic import GROUPS, SLOW, TYPES, Block, Graph, LogicError, Param, chains, clean

from . import theme

WIDTH = 204.0
HEAD = 24.0
BODY = 32.0
ROW = 20.0
PORT = 6.0
STEP_X, STEP_Y = 260.0, 104.0
GROUP_COLOUR = {"when": theme.ACCENT, "if": theme.ATTENTION, "do": theme.SUCCESS}
OUTPUT_COLOUR = {"then": theme.FG_MUTED, "else": theme.DANGER, "a": theme.DANGER, "b": theme.ACCENT, "c": theme.SUCCESS,
                 "d": theme.ATTENTION, "none": theme.FG_SUBTLE}
EMPTY = "[play]\ntitle = \"Interactive show\"\n"


def block_height(block: Block) -> float:
    return HEAD + BODY + ROW * max(0, len(block.kind.outputs) - 1)


class BlockItem(QGraphicsItem):
    """One block: a title bar in the colour of its group, what it does in a line, a dot on the left
    (what leads to it) and a dot per output on the right."""

    def __init__(self, block: Block, on_moved: Callable[[BlockItem], None]) -> None:
        super().__init__()
        self.block = block
        self.on_moved = on_moved
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
                      | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setZValue(2.0)
        self.setToolTip(f"{block.kind.title}: {block.kind.summary}")
        if block.pos is not None:
            self.setPos(block.pos[0], block.pos[1])

    @property
    def height(self) -> float:
        return block_height(self.block)

    def boundingRect(self) -> QRectF:
        return QRectF(-PORT - 2.0, -2.0, WIDTH + 2.0 * PORT + 4.0, self.height + 4.0)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def input_point(self) -> QPointF:
        return self.mapToScene(QPointF(0.0, HEAD + BODY / 2.0))

    def output_point(self, output: str) -> QPointF:
        outputs = self.block.kind.outputs
        k = outputs.index(output) if output in outputs else 0
        return self.mapToScene(QPointF(WIDTH, HEAD + BODY / 2.0 + ROW * k))

    def output_at(self, scene_pos: QPointF) -> str | None:
        """The output whose dot is at this point of the scene, if any."""
        for output in self.block.kind.outputs:
            p = self.output_point(output)
            if abs(p.x() - scene_pos.x()) <= PORT + 5.0 and abs(p.y() - scene_pos.y()) <= ROW / 2.0:
                return output
        return None

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        block, kind, h = self.block, self.block.kind, self.height
        colour = QColor(GROUP_COLOUR[kind.group])
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QPainterPath()
        body.addRoundedRect(QRectF(0.0, 0.0, WIDTH, h), 6.0, 6.0)
        painter.fillPath(body, QColor(theme.SUBTLE))
        painter.save()
        painter.setClipPath(body)
        head = QColor(colour)
        head.setAlpha(70)
        painter.fillRect(QRectF(0.0, 0.0, WIDTH, HEAD), head)
        painter.fillRect(QRectF(0.0, 0.0, 4.0, h), colour)
        painter.restore()
        painter.setPen(QPen(QColor(theme.ACCENT if self.isSelected() else theme.BORDER), 2.0 if self.isSelected() else 1.0))
        painter.drawPath(body)
        painter.setFont(theme.font(9.0, bold=True))
        painter.setPen(QColor(theme.FG))
        metrics = painter.fontMetrics()
        painter.drawText(QRectF(12.0, 0.0, WIDTH - 20.0, HEAD), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         metrics.elidedText(kind.title, Qt.TextElideMode.ElideRight, int(WIDTH - 20.0)))
        painter.setFont(theme.font(8.5))
        metrics = painter.fontMetrics()
        many = len(kind.outputs) > 1
        room = WIDTH - 24.0 - (44.0 if many else 0.0)
        try:
            words = block.label()
        except (KeyError, TypeError, ValueError):
            words = ""
        painter.setPen(QColor(theme.FG_MUTED))
        painter.drawText(QRectF(12.0, HEAD, room, BODY), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                         metrics.elidedText(words, Qt.TextElideMode.ElideRight, int(room)))
        painter.setPen(QColor(theme.FG_SUBTLE))
        if h > HEAD + BODY + 4.0:
            painter.drawText(QRectF(12.0, HEAD + BODY, room, ROW), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                             metrics.elidedText(block.id, Qt.TextElideMode.ElideRight, int(room)))
        if kind.has_input:
            painter.setPen(QPen(QColor(theme.BORDER), 1.0))
            painter.setBrush(QBrush(QColor(theme.CANVAS)))
            painter.drawEllipse(QPointF(0.0, HEAD + BODY / 2.0), PORT, PORT)
        for k, output in enumerate(kind.outputs):
            y = HEAD + BODY / 2.0 + ROW * k
            dot = QColor(OUTPUT_COLOUR.get(output, theme.FG_MUTED))
            if many:
                painter.setPen(dot)
                painter.drawText(QRectF(WIDTH - 52.0, y - ROW / 2.0, 40.0, ROW),
                                 int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), output)
            painter.setPen(QPen(QColor(theme.CANVAS), 1.0))
            painter.setBrush(QBrush(dot))
            painter.drawEllipse(QPointF(WIDTH, y), PORT, PORT)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:      # whole pixels: what the file holds
            return QPointF(round(value.x()), round(value.y()))
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.block.pos = (self.pos().x(), self.pos().y())
            self.on_moved(self)
        return super().itemChange(change, value)


def wire_path(start: QPointF, end: QPointF) -> QPainterPath:
    path = QPainterPath(start)
    reach = max(40.0, abs(end.x() - start.x()) * 0.5)
    path.cubicTo(QPointF(start.x() + reach, start.y()), QPointF(end.x() - reach, end.y()), end)
    return path


class WireItem(QGraphicsPathItem):
    def __init__(self, source: str, output: str, target: str) -> None:
        super().__init__()
        self.source, self.output, self.target = source, output, target
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setZValue(1.0)

    def shape(self) -> QPainterPath:               # easier to hit than a line of two pixels
        stroker = QPainterPathStroker()
        stroker.setWidth(12.0)
        return stroker.createStroke(self.path())

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(theme.ACCENT if self.isSelected() else OUTPUT_COLOUR.get(self.output, theme.FG_MUTED))
        painter.setPen(QPen(colour, 3.0 if self.isSelected() else 2.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())


class GraphScene(QGraphicsScene):
    """The picture of a graph. The window owns the graph and tells the scene to draw it again."""

    def __init__(self, window: InteractionWindow) -> None:
        super().__init__(window)
        self.window = window
        self.items_by_id: dict[str, BlockItem] = {}
        self.wires: list[WireItem] = []
        self._pull: tuple[str, str] | None = None            # a wire is being pulled from (block, output)
        self._rubber: QGraphicsPathItem | None = None
        self._moved = False
        self.setBackgroundBrush(QBrush(QColor(theme.CANVAS)))

    # ------------------------------------------------------------------ drawing the graph
    def rebuild(self, graph: Graph) -> None:
        selected = {i.block.id for i in self.items_by_id.values() if i.isSelected()}
        self.items_by_id, self.wires, self._pull, self._rubber = {}, [], None, None
        self.clear()
        for block in graph.blocks.values():
            item = BlockItem(block, self._block_moved)
            self.addItem(item)
            self.items_by_id[block.id] = item
        for block in graph.blocks.values():
            for output, targets in block.then.items():
                for target in targets:
                    if target in self.items_by_id:
                        wire = WireItem(block.id, output, target)
                        self.addItem(wire)
                        self.wires.append(wire)
        self.route()
        for block_id in selected:
            if block_id in self.items_by_id:
                self.items_by_id[block_id].setSelected(True)

    def route(self, only: str | None = None) -> None:
        for wire in self.wires:
            if only is not None and only not in (wire.source, wire.target):
                continue
            a, b = self.items_by_id.get(wire.source), self.items_by_id.get(wire.target)
            if a is not None and b is not None:
                wire.setPath(wire_path(a.output_point(wire.output), b.input_point()))

    def _block_moved(self, item: BlockItem) -> None:
        self._moved = True
        self.route(item.block.id)

    def block_at(self, pos: QPointF) -> BlockItem | None:
        for item in self.items(pos):
            if isinstance(item, BlockItem):
                return item
        return None

    # ------------------------------------------------------------------ pulling a wire
    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.block_at(event.scenePos())
            output = item.output_at(event.scenePos()) if item is not None else None
            if item is not None and output is not None:
                self._pull = (item.block.id, output)
                self._rubber = QGraphicsPathItem()
                self._rubber.setPen(QPen(QColor(theme.ACCENT), 2.0, Qt.PenStyle.DashLine))
                self._rubber.setZValue(3.0)
                self.addItem(self._rubber)
                event.accept()
                return
            self._moved = False
            self.window.remember()                          # a block may be about to be moved
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._pull is not None and self._rubber is not None:
            start = self.items_by_id[self._pull[0]].output_point(self._pull[1])
            self._rubber.setPath(wire_path(start, event.scenePos()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._pull is not None:
            source, output = self._pull
            if self._rubber is not None:
                self.removeItem(self._rubber)
            self._pull, self._rubber = None, None
            target = self.block_at(event.scenePos())
            if target is not None:
                self.window.connect_blocks(source, output, target.block.id)
            event.accept()
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.window.moved(self._moved)
            self._moved = False


class GraphView(QGraphicsView):
    def __init__(self, scene: GraphScene, window: InteractionWindow) -> None:
        super().__init__(scene, window)
        self.window_ = window
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setAccessibleName("The blocks and their wires")
        self._pan: QPointF | None = None

    def drawBackground(self, painter: QPainter, area: Any) -> None:
        rect = QRectF(area)
        painter.fillRect(rect, QColor(theme.CANVAS))
        step = 26.0
        painter.setPen(QPen(QColor(theme.BORDER_MUTED), 1.5))
        x0, y0 = int(rect.left() // step) * step, int(rect.top() // step) * step
        y = y0
        while y < rect.bottom():
            x = x0
            while x < rect.right():
                painter.drawPoint(QPointF(x, y))
                x += step
            y += step

    def zoom(self, factor: float) -> None:
        now = self.transform().m11()
        target = min(2.5, max(0.2, now * factor))
        self.scale(target / now, target / now)

    def fit(self) -> None:
        items = self.scene().itemsBoundingRect()
        if items.isEmpty():
            self.setTransform(QTransform())
            return
        self.fitInView(items.adjusted(-60.0, -60.0, 60.0, 60.0), Qt.AspectRatioMode.KeepAspectRatio)
        if self.transform().m11() > 1.0:
            self.setTransform(QTransform())
            self.centerOn(items.center())

    def wheelEvent(self, event: Any) -> None:
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15)

    def mousePressEvent(self, event: Any) -> None:
        middle = event.button() == Qt.MouseButton.MiddleButton
        alt = event.button() == Qt.MouseButton.LeftButton and event.modifiers() & Qt.KeyboardModifier.AltModifier
        if middle or alt:
            self._pan = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._pan is not None:
            delta = event.position() - self._pan
            self._pan = event.position()
            self.horizontalScrollBar().setValue(int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(int(self.verticalScrollBar().value() - delta.y()))
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._pan is not None:
            self._pan = None
            self.unsetCursor()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event: Any) -> None:
        self.window_.context_menu(self.mapToScene(event.pos()), event.globalPos())

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):      # here, not in a text field of the panel
            self.window_.delete_selection()
            return
        super().keyPressEvent(event)


def tidy(graph: Graph) -> None:
    """Positions for every block: columns by how far a block is from a 'when' block; a block is put no
    higher than the block that leads to it and every chain gets a band of its own, so that it reads from
    left to right."""
    depth: dict[str, int] = {}
    parent: dict[str, str] = {}
    queue = [b.id for b in graph.blocks.values() if b.kind.group == "when"]
    for block_id in queue:
        depth[block_id] = 0
    while queue:
        block_id = queue.pop(0)
        for targets in graph.blocks[block_id].then.values():
            for target in targets:
                if target in graph.blocks and target not in depth:
                    depth[target] = depth[block_id] + 1
                    parent[target] = block_id
                    queue.append(target)
    lonely = max(depth.values(), default=-1) + 1
    order: list[str] = []

    def visit(block_id: str) -> None:                    # each chain as a whole, in the order it is written
        order.append(block_id)
        for targets in graph.blocks[block_id].then.values():
            for target in targets:
                if parent.get(target) == block_id:
                    visit(target)

    for block in graph.blocks.values():
        if block.kind.group == "when":
            visit(block.id)
    order += [block_id for block_id in graph.blocks if block_id not in depth]
    free: dict[int, float] = {}
    placed: dict[str, float] = {}
    for block_id in order:
        block = graph.blocks[block_id]
        column = depth.get(block_id, lonely)
        if block_id in parent:
            y = max(free.get(column, 0.0), placed[parent[block_id]])
        else:                                            # a new chain (or a block on its own): below everything so far
            y = max(free.values(), default=0.0)
        block.pos = (column * STEP_X, y)
        placed[block_id] = y
        free[column] = y + max(STEP_Y, block_height(block) + 34.0)


def levels_text(levels: dict[str, float]) -> str:
    return ", ".join(f"{name} = {level:g}" for name, level in levels.items())


def parse_levels(text: str) -> dict[str, float]:
    levels: dict[str, float] = {}
    for part in text.replace(";", ",").split(","):
        if not part.strip():
            continue
        name, sep, level = part.partition("=")
        if not sep:
            name, level = part, "1"
        try:
            levels[name.strip()] = float(level)
        except ValueError as exc:
            raise LogicError(f"'{part.strip()}': write name = level, e.g. vibration = 1") from exc
    return levels


class InteractionWindow(QWidget):
    """The kinds of blocks on the left, the blocks in the middle, the chosen block's settings on the right."""

    scene: GraphScene
    view: GraphView

    def __init__(self, main: Any = None) -> None:
        super().__init__(None)
        self.main = main
        self.graph = Graph()
        self.path = ""
        self.dirty = False
        self.seats: list[str] = []
        self.teams: list[str] = []
        self._undo: list[list[dict[str, Any]]] = []
        self._redo: list[list[dict[str, Any]]] = []
        self._held: list[dict[str, Any]] | None = None
        self._building = False
        self.ask_save_name: Callable[[], str] = self._ask_save_name
        self.ask_discard: Callable[[], str] = self._ask_discard
        self.setWindowTitle("Interaction")
        self._build()
        theme.adopt(self)
        self.resize(1360, 820)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.status.setText(""))
        self.scene.selectionChanged.connect(self._selection_changed)
        self.refresh()

    # ------------------------------------------------------------------ building the window
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        bar = QHBoxLayout()
        for text, slot, tip in (("New", self.new, "An empty set of blocks"),
                                ("Open…", self.open_dialog, "The blocks of a play.toml")):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            bar.addWidget(button)
        self.btn_save = QPushButton("Save", self)
        self.btn_save.setObjectName("primary")
        self.btn_save.setToolTip("Write the blocks into the play.toml; nothing else of the file is changed (Ctrl+S)")
        self.btn_save.clicked.connect(self.save)
        bar.addWidget(self.btn_save)
        save_as = QPushButton("Save as…", self)
        save_as.clicked.connect(self.save_as)
        bar.addWidget(save_as)
        bar.addSpacing(14)
        self.btn_undo = QPushButton("Undo", self)
        self.btn_undo.clicked.connect(self.undo)
        bar.addWidget(self.btn_undo)
        self.btn_redo = QPushButton("Redo", self)
        self.btn_redo.clicked.connect(self.redo)
        bar.addWidget(self.btn_redo)
        bar.addSpacing(14)
        self.btn_delete = QPushButton("Delete", self)
        self.btn_delete.setToolTip("Remove the chosen blocks and wires (Del)")
        self.btn_delete.clicked.connect(self.delete_selection)
        bar.addWidget(self.btn_delete)
        tidy_button = QPushButton("Tidy", self)
        tidy_button.setToolTip("Put the blocks in columns, in the order things happen")
        tidy_button.clicked.connect(self.tidy)
        bar.addWidget(tidy_button)
        fit_button = QPushButton("Fit", self)
        fit_button.setToolTip("Show all blocks (Ctrl+0)")
        fit_button.clicked.connect(lambda: self.view.fit())
        bar.addWidget(fit_button)
        bar.addStretch(1)
        self.file_label = QLabel("", self)
        self.file_label.setObjectName("muted")
        bar.addWidget(self.file_label)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal, self)
        left = QWidget(split)
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("BLOCKS  (double-click to add)", left)
        heading.setObjectName("muted")
        left_box.addWidget(heading)
        self.kinds = QListWidget(left)
        self.kinds.setAccessibleName("Kinds of blocks")
        self.kinds.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.kinds.setTextElideMode(Qt.TextElideMode.ElideRight)
        for group, title in GROUPS.items():
            head = QListWidgetItem(title.upper())
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            head.setForeground(QColor(GROUP_COLOUR[group]))
            self.kinds.addItem(head)
            for kind in TYPES.values():
                if kind.group == group:
                    entry = QListWidgetItem("  " + kind.title)
                    entry.setData(Qt.ItemDataRole.UserRole, kind.name)
                    entry.setToolTip(kind.summary)
                    self.kinds.addItem(entry)
        self.kinds.itemActivated.connect(self._palette_activated)
        self.kinds.itemDoubleClicked.connect(self._palette_activated)
        left_box.addWidget(self.kinds, 1)
        split.addWidget(left)

        middle = QSplitter(Qt.Orientation.Vertical, split)
        self.scene = GraphScene(self)
        self.view = GraphView(self.scene, self)
        middle.addWidget(self.view)
        below = QSplitter(Qt.Orientation.Horizontal, middle)
        self.notes = QListWidget(below)
        self.notes.setAccessibleName("Notes about the blocks")
        self.notes.itemActivated.connect(self._note_activated)
        self.notes.itemClicked.connect(self._note_activated)
        below.addWidget(self.notes)
        self.words = QPlainTextEdit(below)
        self.words.setReadOnly(True)
        self.words.setAccessibleName("The logic in words")
        self.words.setPlaceholderText("The logic in words appears here.")
        below.addWidget(self.words)
        below.setSizes([420, 520])
        middle.addWidget(below)
        middle.setStretchFactor(0, 1)
        middle.setStretchFactor(1, 0)
        middle.setSizes([600, 170])
        split.addWidget(middle)

        scroll = QScrollArea(split)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(300)
        self.props = QWidget(scroll)
        self.props_box = QVBoxLayout(self.props)
        self.props_box.setContentsMargins(8, 4, 8, 8)
        scroll.setWidget(self.props)
        split.addWidget(scroll)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([250, 760, 350])
        outer.addWidget(split, 1)
        self.status = QLabel("", self)
        self.status.setObjectName("muted")
        outer.addWidget(self.status)
        keys_and_slots: tuple[tuple[str, Callable[[], Any]], ...] = (
            ("Ctrl+S", self.save), ("Ctrl+Z", self.undo), ("Ctrl+Shift+Z", self.redo), ("Ctrl+Y", self.redo),
            ("Ctrl+D", self.duplicate_selection),
            ("Ctrl+0", self.view.fit), ("Ctrl++", lambda: self.view.zoom(1.25)), ("Ctrl+-", lambda: self.view.zoom(0.8)))
        for keys, slot in keys_and_slots:
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(slot)

    # ------------------------------------------------------------------ messages
    def say(self, text: str, seconds: float = 6.0) -> None:
        self.status.setText(text)
        self._status_timer.start(int(seconds * 1000))

    def _title(self) -> None:
        name = Path(self.path).name if self.path else "not saved yet"
        self.setWindowTitle(f"Interaction - {name}{' *' if self.dirty else ''}")
        self.file_label.setText((self.path or "no file") + ("   (changed)" if self.dirty else ""))
        self.btn_undo.setEnabled(bool(self._undo))
        self.btn_redo.setEnabled(bool(self._redo))

    # ------------------------------------------------------------------ undo
    def snapshot(self) -> list[dict[str, Any]]:
        return self.graph.to_list()

    def remember(self) -> None:
        """Keep the present state: the next change can be undone to it (see `moved`)."""
        self._held = self.snapshot()

    def moved(self, really: bool) -> None:
        if really and self._held is not None:
            self._commit(self._held, redraw=False)
        self._held = None

    def _commit(self, before: list[dict[str, Any]], redraw: bool = True, props: bool = True) -> None:
        self._undo.append(before)
        del self._undo[:-100]
        self._redo.clear()
        self.dirty = True
        if redraw:
            self.refresh(props)
        else:
            self._title()

    def _restore(self, state: list[dict[str, Any]]) -> None:
        self.graph = Graph.from_list(state, lenient=True)
        self.dirty = True
        self.refresh()

    def undo(self) -> None:
        if self._undo:
            self._redo.append(self.snapshot())
            self._restore(self._undo.pop())

    def redo(self) -> None:
        if self._redo:
            self._undo.append(self.snapshot())
            self._restore(self._redo.pop())

    # ------------------------------------------------------------------ editing
    def selected_blocks(self) -> list[str]:
        return [i.block.id for i in self.scene.items_by_id.values() if i.isSelected()]

    def select(self, block_id: str | None) -> None:
        self.scene.clearSelection()
        item = self.scene.items_by_id.get(block_id or "")
        if item is not None:
            item.setSelected(True)
            self.view.ensureVisible(item)

    def add_block(self, type_name: str, pos: tuple[float, float] | None = None) -> Block:
        before = self.snapshot()
        if pos is None:
            centre = self.view.mapToScene(self.view.viewport().rect().center())
            taken = {(round(b.pos[0]), round(b.pos[1])) for b in self.graph.blocks.values() if b.pos is not None}
            x, y = round(centre.x() - WIDTH / 2.0), round(centre.y() - 30.0)
            while (x, y) in taken:
                x, y = x + 26, y + 26
            pos = (float(x), float(y))
        block = self.graph.add(type_name, pos)
        self._commit(before)
        self.select(block.id)
        self.say(f"added: {block.kind.title.lower()} ({block.id})")
        return block

    def remove_block(self, block_id: str) -> None:
        if block_id in self.graph.blocks:
            before = self.snapshot()
            self.graph.remove(block_id)
            self._commit(before)

    def delete_selection(self) -> None:
        wires = [w for w in self.scene.wires if w.isSelected()]
        blocks = self.selected_blocks()
        if not wires and not blocks:
            return
        before = self.snapshot()
        for wire in wires:
            self.graph.disconnect(wire.source, wire.output, wire.target)
        for block_id in blocks:
            self.graph.remove(block_id)
        self._commit(before)
        self.say(f"removed {len(blocks)} block(s) and {len(wires)} wire(s)" if blocks else f"removed {len(wires)} wire(s)")

    def duplicate_selection(self) -> None:
        chosen = self.selected_blocks()
        if not chosen:
            return
        before = self.snapshot()
        made = []
        for block_id in chosen:
            old = self.graph.blocks[block_id]
            pos = (old.pos[0] + 30.0, old.pos[1] + 30.0) if old.pos is not None else None
            new = self.graph.add(old.type, pos)
            new.params = {k: (dict(v) if isinstance(v, dict) else v) for k, v in old.params.items()}
            made.append(new.id)
        self._commit(before)
        self.scene.clearSelection()
        for block_id in made:
            self.scene.items_by_id[block_id].setSelected(True)

    def connect_blocks(self, source: str, output: str, target: str) -> bool:
        problem = self.graph.can_connect(source, output, target)
        if problem:
            self.say(f"not connected: {problem}")
            return False
        before = self.snapshot()
        self.graph.connect(source, output, target)
        self._commit(before)
        return True

    def disconnect_blocks(self, source: str, output: str, target: str) -> None:
        before = self.snapshot()
        self.graph.disconnect(source, output, target)
        self._commit(before)

    def rename(self, old: str, new: str) -> bool:
        new = new.strip()
        if new == old or old not in self.graph.blocks:      # (a line edit says "finished" twice: Enter, then losing the focus)
            return new == old
        before = self.snapshot()
        try:
            self.graph.rename(old, new)
        except LogicError as exc:
            self.say(str(exc))
            self._show_props()
            return False
        self._commit(before)
        self.select(new)
        return True

    def set_param(self, block_id: str, name: str, value: Any) -> bool:
        block = self.graph.blocks.get(block_id)
        param = block.kind.param(name) if block is not None else None
        if block is None or param is None:
            return False
        lenient = Param(param.name, param.kind, param.default, param.label, param.options, param.low, param.high, False, param.unit)
        try:
            cleaned = clean(lenient, value, f"block {block_id}")
        except LogicError as exc:
            self.say(str(exc))
            self._show_props()
            return False
        if cleaned == block.params[name]:
            return True
        before = self.snapshot()
        block.params[name] = cleaned
        self._commit(before, props=False)                   # the field that was just edited stays as it is, with the focus
        return True

    def tidy(self) -> None:
        before = self.snapshot()
        tidy(self.graph)
        self._commit(before)
        self.view.fit()

    def context_menu(self, scene_pos: QPointF, global_pos: Any) -> None:
        menu = QMenu(self)
        item = self.scene.block_at(scene_pos)
        if item is not None:
            if not item.isSelected():
                self.select(item.block.id)
            menu.addAction("Duplicate", self.duplicate_selection)
            menu.addAction("Delete", self.delete_selection)
            menu.addSeparator()
        for group, title in GROUPS.items():
            sub = menu.addMenu(f"Add: {title}")
            for kind in TYPES.values():
                if kind.group == group:
                    sub.addAction(kind.title, self._adder(kind.name, (scene_pos.x(), scene_pos.y())))
        menu.exec(global_pos)

    def _adder(self, type_name: str, pos: tuple[float, float]) -> Callable[[], None]:
        def add() -> None:
            self.add_block(type_name, pos)

        return add

    # ------------------------------------------------------------------ files
    def new(self) -> None:
        if not self._may_discard():
            return
        self.graph, self.path, self.dirty, self.seats, self.teams = Graph(), "", False, [], []
        self._undo.clear()
        self._redo.clear()
        self.refresh()
        self.view.fit()

    def open_dialog(self) -> None:
        if not self._may_discard():
            return
        name, _f = QFileDialog.getOpenFileName(self, "Open an interactive show", str(Path(self.path).parent) if self.path else "",
                                               "Interactive show (*.toml);;All files (*)")
        if name:
            try:
                self.open_path(name)
            except PlayConfigError as exc:
                QMessageBox.warning(self, "Interaction", str(exc))

    def open_path(self, path: str | Path) -> None:
        """Read the blocks of a play.toml. Raises PlayConfigError if they cannot be read."""
        p = Path(path).expanduser()
        try:
            data = tomllib.loads(p.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise PlayConfigError(f"{p}: {exc}") from exc
        play = data.get("play", {})
        if not isinstance(play, dict):
            raise PlayConfigError(f"{p}: [play] must be a table")
        try:
            graph = Graph.from_list(play.get("blocks", []), lenient=True)
        except LogicError as exc:
            raise PlayConfigError(f"{p}: {exc}") from exc
        seats = [s for s in play.get("seats", []) if isinstance(s, dict)]
        self.seats = [str(s.get("id", "")) for s in seats if s.get("id")]
        self.teams = sorted({str(s["team"]) for s in seats if s.get("team")})
        if any(b.pos is None for b in graph.blocks.values()):
            tidy(graph)
        self.graph, self.path, self.dirty = graph, str(p), False
        self._undo.clear()
        self._redo.clear()
        self.refresh()
        self.view.fit()
        self.say(f"{len(graph.blocks)} block(s) from {p.name}")

    def save(self) -> bool:
        if not self.path:
            return self.save_as()
        return self.save_to(self.path)

    def save_as(self) -> bool:
        name = self.ask_save_name()
        return bool(name) and self.save_to(name)

    def save_to(self, path: str | Path) -> bool:
        """Write the blocks. Returns False (and says why) if they cannot be written as they are."""
        missing = self.graph.incomplete()
        if missing:
            self.select(missing[0][0])
            self.say(f"not saved: {missing[0][1]}", 12.0)
            return False
        p = Path(path).expanduser()
        created = not p.exists()
        try:
            if created:
                p.write_text(EMPTY, encoding="utf-8")
            save_blocks(p, self.graph)
        except (PlayConfigError, LogicError, OSError) as exc:
            if created:
                p.unlink(missing_ok=True)
            self.say(f"not saved: {exc}", 12.0)
            return False
        self.path, self.dirty = str(p), False
        self._title()
        note = ""
        try:
            cfg = load_play_config(p)
            note = f"; {len(cfg.notes)} note(s) - 'fourdvr play check' lists them" if cfg.notes else ""
        except PlayConfigError as exc:                    # e.g. a film block without [play.film]: saved, but it cannot run
            note = f"; this show cannot run yet: {str(exc).split(': ', 1)[-1]}"
        self.say(f"saved {p.name}{note}", 12.0)
        return True

    def _ask_save_name(self) -> str:
        name, _f = QFileDialog.getSaveFileName(self, "Save the blocks into", self.path or "play.toml", "Interactive show (*.toml)")
        return name

    def _ask_discard(self) -> str:
        box = QMessageBox(QMessageBox.Icon.Question, "Interaction", "The blocks have changes that are not saved.",
                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel, self)
        choice = box.exec()
        return {int(QMessageBox.StandardButton.Save): "save", int(QMessageBox.StandardButton.Discard): "discard"}.get(int(choice), "cancel")

    def _may_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = self.ask_discard()
        return answer == "discard" or (answer == "save" and self.save())

    def closeEvent(self, event: Any) -> None:
        if self._may_discard():
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------------ showing the state
    def refresh(self, props: bool = True) -> None:
        self._building = True                               # the scene's selection comes and goes while it is drawn again
        try:
            self.scene.rebuild(self.graph)
        finally:
            self._building = False
        self._title()
        if props:
            self._show_props()
        self.notes.clear()
        for block_id, text in self.graph.incomplete():
            entry = QListWidgetItem("! " + text)
            entry.setData(Qt.ItemDataRole.UserRole, block_id)
            entry.setForeground(QColor(theme.DANGER))
            self.notes.addItem(entry)
        for text in self.graph.notes():
            entry = QListWidgetItem(text)
            parts = text.split()
            entry.setData(Qt.ItemDataRole.UserRole, parts[1].rstrip(":") if len(parts) > 1 and parts[0] == "block" else "")
            entry.setForeground(QColor(theme.ATTENTION))
            self.notes.addItem(entry)
        if self.notes.count() == 0:
            entry = QListWidgetItem("no notes" if self.graph.blocks else "Add a 'When' block from the list on the left, then say what follows it.")
            entry.setForeground(QColor(theme.FG_SUBTLE))
            self.notes.addItem(entry)
        self.words.setPlainText("\n".join(chains(self.graph, 200)))

    def _note_activated(self, entry: QListWidgetItem) -> None:
        block_id = entry.data(Qt.ItemDataRole.UserRole)
        if block_id:
            self.select(str(block_id))

    def _palette_activated(self, entry: QListWidgetItem) -> None:
        name = entry.data(Qt.ItemDataRole.UserRole)
        if name:
            self.add_block(str(name))

    def _selection_changed(self) -> None:
        if not self._building:
            self._show_props()

    # ------------------------------------------------------------------ the chosen block's settings
    def _clear_props(self) -> None:
        while self.props_box.count():
            item = self.props_box.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _show_props(self) -> None:
        self._building = True
        try:
            self._clear_props()
            chosen = self.selected_blocks()
            self.btn_delete.setEnabled(bool(chosen) or any(w.isSelected() for w in self.scene.wires))
            if len(chosen) != 1 or chosen[0] not in self.graph.blocks:
                hint = QLabel("Choose one block to set it up." if self.graph.blocks else
                              "No blocks yet.\n\nDouble-click a kind of block on the left.", self.props)
                hint.setObjectName("muted")
                hint.setWordWrap(True)
                self.props_box.addWidget(hint)
                self.props_box.addStretch(1)
                return
            block = self.graph.blocks[chosen[0]]
            self._fill_props(block)
        finally:
            self._building = False
            theme.adopt(self.props)

    def _fill_props(self, block: Block) -> None:
        kind = block.kind
        title = QLabel(kind.title, self.props)
        title.setFont(theme.font(11.0, bold=True))
        self.props_box.addWidget(title)
        summary = QLabel(kind.summary, self.props)
        summary.setObjectName("muted")
        summary.setWordWrap(True)
        self.props_box.addWidget(summary)
        holder = QWidget(self.props)
        form = QFormLayout(holder)
        form.setContentsMargins(0, 8, 0, 8)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)      # the name of a setting above its field: room for both
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(4)
        name = QLineEdit(block.id, holder)
        name.setToolTip("The block's name: what 'then' refers to in the file")
        name.setAccessibleName("Name of the block")
        name.editingFinished.connect(lambda: self.rename(block.id, name.text()))
        form.addRow("name", name)
        for param in kind.params:
            form.addRow(param.label + (f"  ({param.unit})" if param.unit and param.kind == "vector" else ""),
                        self._editor(holder, block, param))
        self.props_box.addWidget(holder)

        wires = QLabel("THEN" if kind.outputs == ("then",) else "WHAT FOLLOWS", self.props)
        wires.setObjectName("muted")
        self.props_box.addWidget(wires)
        for output in kind.outputs:
            for target in block.then.get(output, []):
                row = QWidget(self.props)
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                other = self.graph.blocks.get(target)
                text = f"{'' if kind.outputs == ('then',) else output + ':  '}{target}" + (f"  ({other.kind.title.lower()})" if other else "")
                label = QLabel(text, row)
                label.setWordWrap(True)
                line.addWidget(label, 1)
                remove = QToolButton(row)
                remove.setText("×")
                remove.setFixedWidth(30)
                remove.setToolTip(f"Remove the wire to {target}")
                remove.setAccessibleName(f"Remove the wire from {output} to {target}")
                remove.clicked.connect(lambda _=False, o=output, t=target: self.disconnect_blocks(block.id, o, t))
                line.addWidget(remove)
                self.props_box.addWidget(row)
            choose = QComboBox(self.props)
            choose.setAccessibleName(f"Connect {output} to a block")
            choose.addItem(f"connect {'' if kind.outputs == ('then',) else output + ' '}to …", "")
            for other in self.graph.blocks.values():
                if other.kind.has_input and other.id not in block.then.get(output, []) and (other.id != block.id or block.type in SLOW):
                    choose.addItem(f"{other.id}  ({other.kind.title.lower()})", other.id)
            choose.setEnabled(choose.count() > 1)
            choose.activated.connect(self._panel_connector(block.id, output, choose))
            self.props_box.addWidget(choose)
        self.props_box.addStretch(1)

    def _panel_connector(self, source: str, output: str, box: QComboBox) -> Callable[[int], None]:
        def connect(_index: int) -> None:
            target = box.currentData()
            if target:
                self.connect_blocks(source, output, str(target))

        return connect

    def _editor(self, parent: QWidget, block: Block, param: Param) -> QWidget:
        value = block.params[param.name]
        bid, name = block.id, param.name
        if param.kind == "choice":
            box = QComboBox(parent)
            for option in param.options:
                box.addItem(option or "(none)", option)
            box.setCurrentIndex(max(0, box.findData(value)))
            box.activated.connect(lambda _index: self.set_param(bid, name, box.currentData()))
            box.setAccessibleName(param.label)
            return box
        if param.kind == "number":
            spin = QDoubleSpinBox(parent)
            spin.setKeyboardTracking(False)
            spin.setDecimals(2)
            spin.setRange(param.low, param.high)
            spin.setSingleStep(0.1 if param.high <= 100.0 else 1.0)
            spin.setValue(float(value))
            if param.unit:
                spin.setSuffix(" " + param.unit)
            spin.valueChanged.connect(lambda v: self.set_param(bid, name, float(v)))
            spin.setAccessibleName(param.label)
            return spin
        if param.kind == "int":
            whole = QSpinBox(parent)
            whole.setKeyboardTracking(False)
            whole.setRange(int(param.low), int(min(param.high, 2_000_000_000)))
            whole.setValue(int(value))
            whole.valueChanged.connect(lambda v: self.set_param(bid, name, int(v)))
            whole.setAccessibleName(param.label)
            return whole
        if param.kind == "vector":
            row = QWidget(parent)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            spins: list[QDoubleSpinBox] = []
            for k in range(3):
                axis = QDoubleSpinBox(row)
                axis.setKeyboardTracking(False)
                axis.setDecimals(2)
                axis.setRange(-param.high, param.high)
                axis.setSingleStep(0.25)
                axis.setValue(float(value[k]))
                axis.setAccessibleName(f"{param.label}, value {k + 1}")
                spins.append(axis)
                line.addWidget(axis)
            for axis in spins:
                axis.valueChanged.connect(lambda _v: self.set_param(bid, name, [s.value() for s in spins]))
            return row
        edit = QLineEdit(parent)
        edit.setAccessibleName(param.label)
        if param.kind == "levels":
            edit.setText(levels_text(value))
            edit.setPlaceholderText("vibration = 1, air = 0.5")
            edit.editingFinished.connect(lambda: self._set_levels(bid, name, edit.text()))
        elif param.kind == "list":
            edit.setText(" | ".join(value))
            edit.setPlaceholderText("Yes | No")
            edit.editingFinished.connect(lambda: self.set_param(bid, name, [c.strip() for c in edit.text().split("|") if c.strip()]))
        elif param.kind == "number?":
            edit.setText("" if value is None else f"{value:g}")
            edit.setPlaceholderText("any")
            edit.editingFinished.connect(lambda: self._set_optional(bid, name, edit.text()))
        else:
            edit.setText(str(value))
            hints = self.seats if name == "seat" else self.teams if name == "team" else []
            if hints:
                edit.setPlaceholderText("any of: " + ", ".join(hints[:6]))
            edit.editingFinished.connect(lambda: self.set_param(bid, name, edit.text()))
        return edit

    def _set_levels(self, block_id: str, name: str, text: str) -> None:
        try:
            self.set_param(block_id, name, parse_levels(text))
        except LogicError as exc:
            self.say(str(exc))
            self._show_props()

    def _set_optional(self, block_id: str, name: str, text: str) -> None:
        try:
            self.set_param(block_id, name, float(text) if text.strip() else None)
        except ValueError:
            self.say(f"'{text.strip()}' is not a number (leave empty for: any)")
            self._show_props()
