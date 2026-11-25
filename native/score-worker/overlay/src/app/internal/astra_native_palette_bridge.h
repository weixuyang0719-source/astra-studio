// SPDX-License-Identifier: GPL-3.0-only
// Isolated adapter to MuseScore Studio 4.6.5's palette/inspector/text APIs.
#pragma once
#include "engraving/dom/articulation.h"
#include "engraving/dom/fingering.h"
#include "engraving/dom/textline.h"
#include "engraving/dom/tremolosinglechord.h"

#include <cmath>
#include <memory>
#include <stdexcept>
#include <vector>

#include <QBuffer>
#include <QColor>
#include <QCoreApplication>
#include <QIcon>
#include <QImage>
#include <QPainter>
#include <QInputMethodEvent>
#include <QJsonArray>
#include <QJsonObject>
#include <QKeyEvent>
#include <QMetaProperty>
#include <QMetaEnum>
#include <QPixmap>
#include <QPointer>
#include <QSet>
#include <QTranslator>

#include "context/iglobalcontext.h"
#include "engraving/dom/clef.h"
#include "engraving/dom/property.h"
#include "notation/inotation.h"
#include "notation/inotationselection.h"
#include "palette/internal/palettecreator.h"
#include "palette/internal/palettecelliconengine.h"
#include "notation/imasternotation.h"
#include "inspector/models/abstractinspectorproxymodel.h"
#include "inspector/models/general/generalsettingsmodel.h"
#include "inspector/models/inspectorlistmodel.h"
#include "inspector/models/propertyitem.h"
#include "project/inotationproject.h"
#include "astra_native_inspector_domains.h"

namespace mu::app::astra_native_panel {

inline void require(bool valid, const char* detail)
{
    if (!valid) {
        throw std::runtime_error(detail);
    }
}

// Reuse the exact default and master tree that PaletteProvider builds in the
// pinned Studio application. Different trees have separate opaque ID ranges.
class PaletteBridge final
{
public:
    QJsonArray catalog()
    {
        ensure();
        QJsonArray groups;
        using Tree = mu::palette::PaletteTree;
        const Tree& defaults = *m_defaults;
        for (size_t i = 0; i < defaults.palettes.size(); ++i) {
            const auto& group = defaults.palettes.at(i);
            if (!group || !pianoGroup(group->type())) {
                continue;
            }
            // The GUI initializes paletteScale to 1.0; console worker skips PaletteModule::onInit.
            const auto grid = group->gridSize();
            groups.append(QJsonObject {
                {"id", QString::number(qulonglong(i))}, {"name", group->name()},
                {"label", group->translatedName()}, {"visible", group->isVisible()},
                {"type", QString::fromLatin1(QMetaEnum::fromType<mu::palette::Palette::Type>().valueToKey(int(group->type())))},
                {"more", group->isVisible() || bundledExtras(group->type())},
                {"width", grid.width()}, {"height", grid.height()}, {"drawGrid", group->drawGrid()},
                {"cells", cellsFor(*group, i)}
            });
        }
        return groups;
    }

    QJsonObject more(const QString& groupId)
    {
        ensure();
        bool valid = false;
        const auto index = groupId.toULongLong(&valid);
        require(valid && index < m_defaults->palettes.size(), "Invalid native palette group");
        const auto& group = m_defaults->palettes.at(size_t(index));
        require(group && pianoGroup(group->type()), "Invalid native palette type");
        if (!m_master) m_master = mu::palette::PaletteCreator::newMasterPaletteTree();
        QJsonArray extras;
        for (size_t i = 0; i < m_master->palettes.size(); ++i) {
            const auto& master = m_master->palettes.at(i);
            if (master && master->type() == group->type()) {
                // Same equivalence criterion as ExcludePaletteCellFilter::acceptCell.
                extras = cellsFor(*master, MasterOffset + i, group.get());
                break;
            }
        }
        return {{"group", groupId}, {"cells", extras}};
    }

    QString icon(const QString& key, int width = 0, int height = 0, double rasterScale = 1.0)
    {
        const auto [group, value] = cell(key);
        const QSize baseSize = group->gridSize();
        // QML stretches columns to fit the panel. It does not enlarge the
        // engraved element; give the original QIconEngine the actual cell rect.
        const int w = width > 0 ? std::clamp(width, 20, 400) : baseSize.width();
        const int h = height > 0 ? std::clamp(height, 20, 160) : baseSize.height();
        QImage glyph(QSize(qRound(w*rasterScale), qRound(h*rasterScale)), QImage::Format_ARGB32_Premultiplied);
        glyph.setDevicePixelRatio(rasterScale);
        glyph.fill(Qt::transparent);
        glyph.setDotsPerMeterX(3780); // 96 logical DPI, like the embedded Qt WebEngine
        glyph.setDotsPerMeterY(3780);
        QPainter painter(&glyph);
        // Same PaletteModel extra magnification at the stock scale of 1.0.
        mu::palette::PaletteCellIconEngine engine(value, group->mag());
        engine.paint(&painter, QRect(0, 0, w, h), QIcon::Normal, QIcon::Off);
        painter.end();
        QBuffer stream;
        require(stream.open(QIODevice::WriteOnly) && glyph.save(&stream, "PNG"),
                "Unable to paint native palette cell");
        return QString::fromLatin1(stream.data().toBase64());
    }

    bool apply(const mu::notation::INotationInteractionPtr& interaction, const QString& key,
               Qt::KeyboardModifiers modifiers)
    {
        const auto [group, value] = cell(key);
        require(pianoCell(value, group->type()), "Item outside the piano notation scope");
        require(interaction && !interaction->selection()->isNone(), "Select a score element first");
        return interaction->applyPaletteElement(value->element.get(), modifiers);
    }

    bool drop(const mu::notation::INotationInteractionPtr& interaction, const QString& key,
              const muse::PointF& pos, Qt::KeyboardModifiers modifiers)
    {
        const auto [group, value] = cell(key);
        require(pianoCell(value, group->type()), "Item outside the piano notation scope");
        require(bool(interaction), "Missing notation interaction");
        const auto bytes = value->element->mimeData().toQByteArray();
        if (!interaction->startDropSingle(bytes)) {
            return false;
        }
        struct Finish {
            mu::notation::INotationInteractionPtr interaction;
            ~Finish() { interaction->endDrop(); }
        } finish { interaction };
        return interaction->updateDropSingle(pos, modifiers) && interaction->dropSingle(pos, modifiers);
    }

private:
    static constexpr size_t MasterOffset = 10000;
    static QString id(size_t group, size_t index)
    {
        return QString::number(qulonglong(group)) + ":" + QString::number(qulonglong(index));
    }

    static QJsonArray cellsFor(const mu::palette::Palette& group, size_t index,
                              const mu::palette::Palette* excluded = nullptr)
    {
        QJsonArray cells;
        for (size_t j = 0; j < group.cells().size(); ++j) {
            const auto& value = group.cells().at(j);
            // Keep the accepted stock workspace's 13 primary panels exactly
            // as shown in Studio. Optional panels expose only applicable cells.
            if (!value || !value->element || !value->visible ||
                (!group.isVisible() && !pianoCell(value, group.type())) ||
                (excluded && excluded->indexOfCell(*value, false) != -1)) {
                continue;
            }
            cells.append(QJsonObject {
                {"id", id(index, j)}, {"name", value->translatedName()}, {"sourceName", value->name},
                {"kind", QString::fromUtf8(value->element->typeName())},
                {"enabled", pianoCell(value, group.type())}
            });
        }
        return cells;
    }

    static bool pianoGroup(mu::palette::Palette::Type type)
    {
        using T = mu::palette::Palette::Type;
        switch (type) {
        case T::Clef: case T::KeySig: case T::TimeSig: case T::Tempo:
        case T::Pitch: case T::Accidental: case T::Dynamic: case T::Articulation:
        case T::Text: case T::Keyboard: case T::Repeat: case T::BarLine: case T::Layout:
        case T::Bracket: case T::Ornament: case T::Breath: case T::GraceNote:
        case T::NoteHead: case T::Arpeggio: case T::Tremolo: case T::Fingering: case T::Beam: case T::Line:
            return true;
        default:
            // In particular Guitar/FretboardDiagram, Harp, Accordion,
            // BagpipeEmbellishment, Handbells and StringTunings.
            return false;
        }
    }

    static bool bundledExtras(mu::palette::Palette::Type type)
    {
        using T = mu::palette::Palette::Type;
        // The remaining optional groups reuse the same creator in both trees,
        // or (fingering) only differ in excluded guitar/lute examples.
        return type == T::Ornament || type == T::Breath || type == T::Line;
    }

    static bool pianoCell(const mu::palette::PaletteCellPtr& cell, mu::palette::Palette::Type group)
    {
        using namespace mu::engraving;
        if (!cell || !cell->element) {
            return false;
        }
        const auto* element = cell->element.get();
        using T = mu::palette::Palette::Type;
        const auto type = element->type();
        if (group == T::Fingering) {
            if (type != ElementType::FINGERING) return false;
            const auto* f = toFingering(element);
            const auto text = f->xmlText();
            return f->textStyleType() == TextStyleType::FINGERING &&
                   (text == u"1" || text == u"2" || text == u"3" || text == u"4" || text == u"5");
        }
        if (group == T::Arpeggio) {
            // The remaining CHORDLINE entries are fall/doit/plop/scoop
            // inflections, distinct from the two native glissando lines.
            return type == ElementType::ARPEGGIO || type == ElementType::GLISSANDO;
        }
        if (group == T::Tremolo) {
            if (type == ElementType::TREMOLO_SINGLECHORD) return !static_cast<const TremoloSingleChord*>(element)->isBuzzRoll();
            if (type == ElementType::ARTICULATION) {
                // PaletteCreator::newTremoloPalette uses native Articulation for these stock subdivisions.
                const auto symbol = toArticulation(element)->symId();
                return symbol == SymId::tremoloDivisiDots2 || symbol == SymId::tremoloDivisiDots3 ||
                       symbol == SymId::tremoloDivisiDots4 || symbol == SymId::tremoloDivisiDots6;
            }
            return type == ElementType::TREMOLO_TWOCHORD;
        }
        if (group == T::Line) {
            // The generic native Lines Let Ring also extends MIDI note-off on
            // piano. Keep Guitar and unrelated guitar-only variants excluded.
            if (type == ElementType::PALM_MUTE || type == ElementType::VIBRATO) return false;
            // The master tree's "VII" text-line example is a fret/barre marking.
            if (type == ElementType::TEXTLINE && toTextLine(element)->beginText() == u"VII") return false;
        }
        if (element->type() == ElementType::CLEF) {
            const auto type = toClef(element)->clefType();
            return type != ClefType::INVALID && ClefInfo::staffGroup(type) == StaffGroup::STANDARD;
        }
        return element->type() != ElementType::INSTRUMENT_CHANGE && element->type() != ElementType::STAFFTYPE_CHANGE;
    }

    std::pair<const mu::palette::Palette*, mu::palette::PaletteCellPtr> cell(const QString& key)
    {
        ensure();
        bool groupOk = false, cellOk = false;
        const auto parts = key.split(':');
        require(parts.size() == 2, "Invalid native palette ID");
        const auto groupId = parts.at(0).toULongLong(&groupOk);
        const auto cellId = parts.at(1).toULongLong(&cellOk);
        const auto& tree = groupId >= MasterOffset ? m_master : m_defaults;
        const auto index = groupId >= MasterOffset ? groupId - MasterOffset : groupId;
        require(groupOk && cellOk && tree && index < tree->palettes.size(), "Invalid native palette group");
        const auto& group = tree->palettes.at(size_t(index));
        require(group && pianoGroup(group->type()) && cellId < group->cells().size(), "Invalid native palette item");
        const auto& result = group->cells().at(size_t(cellId));
        require(result && result->element && result->visible, "Invalid native palette cell");
        return {group.get(), result};
    }

    void ensure()
    {
        if (m_defaults) return;
        const QString locale = QCoreApplication::applicationDirPath() + "/../locale/musescore_zh_CN.qm";
        // Official pinned translation catalogue, kept alive while this worker owns the palette.
        if (m_translator.load(locale)) QCoreApplication::installTranslator(&m_translator);
        m_defaults = mu::palette::PaletteCreator::newDefaultPaletteTree();
        require(bool(m_defaults), "Missing upstream default palette");
    }

    QTranslator m_translator;
    mu::palette::PaletteTreePtr m_defaults;
    mu::palette::PaletteTreePtr m_master;
};

#if defined(ASTRA_PANEL_EXPERIMENTAL_INSPECTOR)
// Exposed controls come from the stock Inspector models. Invoke the
// PropertyItem itself, which owns unit conversion, multi-element handling,
// optional undo commands and reset. Do not write raw Score property IDs.
class PropertyBridge final
{
public:
    QJsonArray catalog(const muse::modularity::ContextPtr& ctx, const mu::notation::INotationPtr& notation,
                       const mu::project::INotationProjectPtr& project, int revision)
    {
        const auto interaction = notation->interaction();
        require(interaction && !interaction->selection()->isNone(), "请先选择要编辑属性的谱面元素");
        require(!interaction->isTextEditingStarted() && !interaction->noteInput()->isNoteInputMode(),
                "请先完成文字或音符录入");
        ensure(ctx, notation, project);
        // The Inspector constructor subscribes but does not hydrate an already
        // selected item; this is the stock synchronization entry point.
        m_model->setInspectorVisible(false);
        m_model->setInspectorVisible(true);
        m_selection = selection(interaction);
        m_items.clear();
        m_seen.clear();
        m_seenItems.clear();
        m_revision = revision;
        ++m_serial;
        QJsonArray result;
        auto roles = m_model->roleNames();
        int role = roles.key("inspectorSectionModel", -1);
        require(role > 0, "Missing upstream Inspector section role");
        for (int i = 0; i < m_model->rowCount(); ++i) {
            auto* model = qobject_cast<mu::inspector::AbstractInspectorModel*>(
                m_model->data(m_model->index(i,0), role).value<QObject*>());
            inspect(model, result, 0, model ? model->title() : QString());
        }
        return result;
    }

    void set(const mu::notation::INotationInteractionPtr& interaction, int revision, const QString& key,
             const QJsonValue& value, bool reset)
    {
        require(m_notation && m_notation->interaction() == interaction && revision == m_revision
                && selection(interaction) == m_selection && interaction && !interaction->selection()->isNone(),
                "谱面选择已改变，请重新读取属性");
        require(!interaction->isTextEditingStarted() && !interaction->noteInput()->isNoteInputMode(),
                "请先完成文字或音符录入");
        for (auto& row : m_items) {
            if (row.key != key) {
                continue;
            }
            auto* item = row.item.data();
            auto* model = row.model.data();
            require(item && model && item->isEnabled()
                    && astra_inspector_domains::visibleInStockControl(model, row.field, item), "原生属性已不可修改");
            const auto domain = astra_inspector_domains::forField(model, row.field, item);
            require(domain && domain->enabled, "原生属性不适用于当前谱面选择");
            if (reset) {
                require(item->propertyId() != mu::engraving::Pid::END && item->isModified(), "谱面属性没有可恢复的默认值");
                item->resetToDefault();
                return;
            }
            const auto native = astra_inspector_domains::fromWire(*domain, item->value(), value);
            require(native.has_value(), "属性值超出此原生控件范围");
            item->setValue(*native);
            return;
        }
        require(false, "属性版本已过期，请刷新");
    }

private:
    struct Item {
        QString key;
        QByteArray field;
        QPointer<mu::inspector::AbstractInspectorModel> model;
        QPointer<mu::inspector::PropertyItem> item;
    };

    static std::vector<const mu::engraving::EngravingItem*> selection(const mu::notation::INotationInteractionPtr& it)
    {
        if (!it || it->selection()->isNone()) {
            return {};
        }
        const auto elements = it->selection()->elements();
        return { elements.begin(), elements.end() };
    }

    void ensure(const muse::modularity::ContextPtr& ctx, const mu::notation::INotationPtr& notation,
                const mu::project::INotationProjectPtr& project)
    {
        if (m_notation == notation && m_model) {
            return;
        }
        auto context = muse::modularity::_ioc(ctx)->resolve<mu::context::IGlobalContext>("astra-native-panel");
        require(context && project && project->masterNotation()
                && project->masterNotation()->notation() == notation, "Missing upstream Inspector master project");
        m_model.reset();
        // Stock Inspector consults currentMasterNotation() as well as
        // currentNotation(). Assign the already-owned project, not a copy.
        context->setCurrentProject(project);
        require(context->currentMasterNotation() && context->currentNotation() == notation,
                "Inspector did not bind to the native master notation");
        m_model = std::make_unique<mu::inspector::InspectorListModel>();
        m_notation = notation;
    }

    // group: the top-level Inspector section (MuseScore's General, Notation,
    // Text...) whose block draws this model's controls.
    void inspect(mu::inspector::AbstractInspectorModel* model, QJsonArray& result, int depth, const QString& group)
    {
        using namespace mu::inspector;
        if (!model || m_seen.contains(model) || depth > 8 || m_items.size() >= 250) {
            return;
        }
        m_seen.insert(model);
        if (auto* general = qobject_cast<GeneralSettingsModel*>(model)) {
            inspect(qobject_cast<AbstractInspectorModel*>(general->appearanceSettingsModel()), result, depth + 1, group);
            inspect(qobject_cast<AbstractInspectorModel*>(general->playbackProxyModel()), result, depth + 1, group);
        }
        if (auto* proxy = qobject_cast<AbstractInspectorProxyModel*>(model)) {
            for (auto* child : proxy->modelList()) {
                inspect(child, result, depth + 1, group);
            }
        }
        const auto* meta = model->metaObject();
        for (const auto* field : astra_inspector_domains::FIELDS) {
            const auto index = meta->indexOfProperty(field);
            if (index < QObject::staticMetaObject.propertyCount()) {
                continue;
            }
            const auto property = meta->property(index);
            // Do not enumerate or execute arbitrary model getters: invoke only
            // whitelisted, constant PropertyItem* bindings from Inspector QML.
            if (!property.isReadable() || !property.isConstant()
                || property.metaType() != QMetaType::fromType<PropertyItem*>()) {
                continue;
            }
            auto* item = property.read(model).value<PropertyItem*>();
            if (!item || !astra_inspector_domains::visibleInStockControl(model, field, item) || m_seenItems.contains(item)) {
                continue;
            }
            const auto domain = astra_inspector_domains::forField(model, field, item);
            if (!domain) {
                continue;
            }
            const auto pid = item->propertyId();
            const auto key = QString::number(m_serial) + ":" + QString::number(qulonglong(m_items.size()));
            m_seenItems.insert(item);
            m_items.push_back({ key, field, model, item });
            auto row = astra_inspector_domains::descriptor(*domain, item);
            row.insert("id",key);
            row.insert("name",domain->label.isEmpty() ? mu::engraving::propertyUserName(pid).toQString() : domain->label);
            row.insert("field",QString::fromLatin1(field));
            row.insert("section",model->title());
            row.insert("group",group);
            row.insert("depth",depth);
            row.insert("editable",row.value("enabled"));
            result.append(row);
        }
    }

    std::unique_ptr<mu::inspector::InspectorListModel> m_model;
    mu::notation::INotationPtr m_notation;
    QSet<mu::inspector::AbstractInspectorModel*> m_seen;
    QSet<mu::inspector::PropertyItem*> m_seenItems;
    std::vector<const mu::engraving::EngravingItem*> m_selection;
    std::vector<Item> m_items;
    int m_serial = 0;
    int m_revision = -1;
};
#endif

inline void textAction(const mu::notation::INotationInteractionPtr& interaction, const QJsonObject& body)
{
    const auto op = body.value("operation").toString();
    if (op == "start") {
        auto* element = interaction->selection()->element();
        require(element && interaction->textEditingAllowed(element), "请选择可编辑的谱面文字");
        interaction->startEditText(element);
        return;
    }
    require(interaction->isTextEditingStarted(), "尚未开始谱面文字编辑");
    if (op == "input") {
        const auto contents = body.value("text").toString();
        require(!contents.isEmpty() && contents.size() <= 1024, "文本输入无效");
        QInputMethodEvent event;
        event.setCommitString(contents);
        interaction->editText(&event);
    } else if (op == "key") {
        const auto name = body.value("key").toString();
        const int key = name == "Backspace" ? Qt::Key_Backspace : name == "Delete" ? Qt::Key_Delete
                        : name == "ArrowLeft" ? Qt::Key_Left : name == "ArrowRight" ? Qt::Key_Right
                        : name == "Enter" ? Qt::Key_Return : 0;
        require(key != 0, "Unsupported native text key");
        QKeyEvent event(QEvent::KeyPress, key, Qt::NoModifier);
        if (interaction->isEditAllowed(&event)) {
            interaction->editElement(&event);
        }
    } else if (op == "finish") {
        interaction->endEditText();
    } else if (op == "bold") {
        interaction->toggleBold();
    } else if (op == "italic") {
        interaction->toggleItalic();
    } else if (op == "underline") {
        interaction->toggleUnderline();
    } else {
        require(false, "Unsupported native text action");
    }
}
} // namespace mu::app::astra_native_panel
