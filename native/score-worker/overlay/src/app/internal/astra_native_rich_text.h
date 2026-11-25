// SPDX-License-Identifier: GPL-3.0-only
// Same-document frontend to pinned MuseScore native text insertion and cursor.
#pragma once

#include <memory>
#include <stdexcept>

#include <QInputMethodEvent>
#include <QJsonObject>
#include <QKeyEvent>

#include "engraving/dom/score.h"
#include "engraving/dom/text.h"
#include "engraving/dom/textbase.h"
#include "notation/inotation.h"
#include "notation/inotationselection.h"
#include "project/inotationproject.h"

namespace mu::app::astra_rich_text {

using mu::engraving::Score;
using mu::engraving::TextBase;
using mu::engraving::TextStyleType;
using mu::notation::INotationInteractionPtr;
using mu::notation::INotationPtr;
using mu::project::INotationProjectPtr;

inline void require(bool ok, const char* error)
{
    if (!ok) {
        throw std::runtime_error(error);
    }
}

inline bool headerStyle(TextStyleType type)
{
    return type == TextStyleType::TITLE || type == TextStyleType::COMPOSER;
}

inline QString styleName(TextStyleType type)
{
    switch (type) {
    case TextStyleType::TITLE: return QStringLiteral("title");
    case TextStyleType::COMPOSER: return QStringLiteral("composer");
    case TextStyleType::STAFF: return QStringLiteral("staff");
    case TextStyleType::SYSTEM: return QStringLiteral("system");
    case TextStyleType::SUBTITLE: return QStringLiteral("subtitle");
    case TextStyleType::LYRICIST: return QStringLiteral("poet");
    case TextStyleType::INSTRUMENT_EXCERPT: return QStringLiteral("part");
    case TextStyleType::FRAME: return QStringLiteral("frame");
    case TextStyleType::EXPRESSION: return QStringLiteral("expression");
    case TextStyleType::REHEARSAL_MARK: return QStringLiteral("rehearsalmark");
    case TextStyleType::FINGERING: return QStringLiteral("fingering");
    case TextStyleType::STICKING: return QStringLiteral("sticking");
    case TextStyleType::HARMONY_A: return QStringLiteral("chord");
    case TextStyleType::HARMONY_ROMAN: return QStringLiteral("roman");
    case TextStyleType::HARMONY_NASHVILLE: return QStringLiteral("nashville");
    case TextStyleType::LYRICS_ODD: case TextStyleType::LYRICS_EVEN: return QStringLiteral("lyrics");
    case TextStyleType::TEMPO: return QStringLiteral("tempo");
    default: return QStringLiteral("other");
    }
}

inline QJsonObject editingInfo(const INotationInteractionPtr& interaction)
{
    if (!interaction->isTextEditingStarted() || !interaction->editedText() || !interaction->editedText()->cursor()) {
        return {};
    }
    const auto* text = interaction->editedText();
    const auto* cursor = text->cursor();
    const auto* format = cursor->format();
    return {{"role",text->isFiguredBass() ? QStringLiteral("figured-bass") : styleName(text->textStyleType())}, {"selected",interaction->isTextSelected()},
            {"bold",format->bold()}, {"italic",format->italic()}, {"underline",format->underline()}, {"strike",format->strike()}};
}

class Session final
{
public:
    bool add(const INotationPtr& notation, const QString& role)
    {
        const auto interaction = notation->interaction();
        require(!interaction->isTextEditingStarted() && !interaction->noteInput()->isNoteInputMode(),
                "请先完成当前录入");
        if (role == "figured-bass") {
            require(bool(interaction->canAddFiguredBass()), "请先选择可添加数字低音的音符或休止符");
            remember(notation, nullptr);
            interaction->addFiguredBass();
            require(interaction->isTextEditingStarted(), "未能进入原生数字低音编辑");
            return false;
        }
        const QMap<QString, TextStyleType> styles {
            {"title",TextStyleType::TITLE}, {"composer",TextStyleType::COMPOSER},
            {"staff",TextStyleType::STAFF}, {"system",TextStyleType::SYSTEM},
            {"subtitle",TextStyleType::SUBTITLE},
            {"poet",TextStyleType::LYRICIST},
            {"part",TextStyleType::INSTRUMENT_EXCERPT},
            {"frame",TextStyleType::FRAME},
            {"expression",TextStyleType::EXPRESSION},
            {"rehearsalmark",TextStyleType::REHEARSAL_MARK},
            {"fingering",TextStyleType::FINGERING},
            {"chord",TextStyleType::HARMONY_A},
            {"roman",TextStyleType::HARMONY_ROMAN},
            {"nashville",TextStyleType::HARMONY_NASHVILLE},
            {"lyrics",TextStyleType::LYRICS_ODD},
            {"tempo",TextStyleType::TEMPO},
        };
        require(styles.contains(role), "不支持的原生文本类型");
        const auto type = styles.value(role);
        const bool topFrame = headerStyle(type) || type == TextStyleType::SUBTITLE ||
                              type == TextStyleType::LYRICIST || type == TextStyleType::INSTRUMENT_EXCERPT;
        Score* score = notation->elements()->msScore();
        const TextBase* prior = headerStyle(type) ? score->getText(type) : nullptr;
        remember(notation, prior);
        if (headerStyle(type)) {
            if (prior) {
                // Reopen the stock title/composer element, never add a duplicate.
                interaction->startEditText(score->getText(type));
            } else {
                interaction->addTextToTopFrame(type);
            }
            m_header = true;
            m_role = type;
        } else if (topFrame) {
            interaction->addTextToTopFrame(type);
        } else {
            auto* anchor = interaction->contextItem();
            const auto selection = interaction->selection();
            if (selection->isRange()) {
                const auto range = selection->range();
                auto* segment = range ? range->rangeStartSegment() : nullptr;
                anchor = segment ? segment->firstElementForNavigation(range->startStaffIndex()) : nullptr;
            }
            require(bool(interaction->canAddTextToItem(type, anchor)), "请选择放置谱面文字的音符或休止符");
            interaction->addTextToItem(type, anchor);
        }
        require(interaction->isTextEditingStarted(), "未能进入原生文字编辑");
        return topFrame;
    }

    // Qt score view uses these same native caret and word-selection methods.
    bool pointer(const INotationPtr& notation, const INotationProjectPtr& project,
                 const muse::PointF& pos, const QString& op)
    {
        using namespace mu::engraving;
        const auto interaction = notation->interaction();
        if (op == "start-at" && !interaction->isTextEditingStarted()) {
            if (interaction->noteInput()->isNoteInputMode()) return false;
            auto hits = interaction->hitElements(pos, 3.f);
            auto hit = std::find_if(hits.rbegin(), hits.rend(),
                                    [&](const auto* item){ return interaction->textEditingAllowed(item); });
            if (hit == hits.rend()) return false;
            auto* original = *hit;
            remember(notation, original->isTextBase() ? toTextBase(original) : nullptr);
            interaction->startEditText(original, pos);
            return false;
        }
        if (!interaction->isTextEditingStarted() || !interaction->editedText()) return false;
        auto* edited = interaction->editedText();
        if (m_notation.lock() != notation) remember(notation, edited);
        if (op == "caret-at") {
            const auto rect = edited->canvasBoundingRect().adjusted(-5.0, -5.0, 5.0, 5.0);
            if (!rect.contains(pos)) return finish(notation, project);
            interaction->changeTextCursorPosition(pos);
        } else if (op == "extend-to") {
            // NotationInteraction's existing text branch invokes TextBase::dragTo
            // and emits text selection state without starting a score undo macro.
            interaction->drag(pos, pos, mu::notation::DragMode::BothXY);
        } else if (op == "start-at" || op == "select-word") {
            interaction->selectText(mu::engraving::SelectTextType::Word);
        } else if (op == "select-text") {
            interaction->selectText(mu::engraving::SelectTextType::All);
        }
        return false;
    }

    bool dispatch(const INotationPtr& notation, const INotationProjectPtr& project, const QJsonObject& body)
    {
        const auto interaction = notation->interaction();
        const auto op = body.value("operation").toString();
        if (op == "start") {
            auto* element = interaction->selection()->element();
            require(!interaction->isTextEditingStarted() && element && interaction->textEditingAllowed(element),
                    "请选择可以编辑的原生文字");
            const auto* text = element->isTextBase() ? mu::engraving::toTextBase(element) : nullptr;
            remember(notation, text);
            interaction->startEditText(element);
            require(interaction->isTextEditingStarted(), "未能进入原生文字编辑");
            return false;
        }
        require(interaction->isTextEditingStarted() && interaction->editedText(), "尚未开始谱面文字编辑");
        if (m_notation.lock() != notation) {
            // Stock palette actions may enter text edit directly.
            remember(notation, interaction->editedText());
        }
        if (op == "ime") {
            const auto commit = body.value("text").toString();
            const auto preedit = body.value("preedit").toString();
            require(commit.size() <= 180 && preedit.size() <= 180
                    && !commit.contains(QChar(ushort(0))) && !preedit.contains(QChar(ushort(0))),
                    "inline text input exceeds one native text packet");
            // Preserve Lane 2's real tempo-note glyph and ordinary Unicode.
            const auto* text = interaction->editedText();
            if (auto* cursor = text->cursor(); cursor && cursor->format()->fontFamily() == u"ScoreText") {
                cursor->format()->setFontFamily(text->propertyDefault(mu::engraving::Pid::FONT_FACE).value<muse::String>());
            }
            if (commit.isEmpty()) {
                QInputMethodEvent event(preedit, {});
                interaction->editText(&event);
            } else {
                // Qt's QInputMethodEvent inserts rather than replacing
                // selection. Clear temporary IME text, then make standard
                // typed replacement against the actual native selection.
                QInputMethodEvent clear;
                interaction->editText(&clear);
                if (interaction->editedText()->cursor()->hasSelection()) interaction->deleteSelection();
                QInputMethodEvent event;
                event.setCommitString(commit);
                interaction->editText(&event);
            }
        } else if (op == "input" || op == "replace") {
            const auto contents = body.value("text").toString();
            require(!contents.isEmpty() && contents.size() <= 512 && !contents.contains(QChar(u'\0')), "文本输入无效");
            if (op == "replace") {
                interaction->selectAll();
                // Pinned IME editText inserts without removing its selection.
                // Explicit whole-text replacement uses native text deletion;
                // ordinary insertion must preserve the native cursor format.
                if (interaction->isTextSelected()) {
                    interaction->deleteSelection();
                }
            }
            // Browser drafts carry ordinary Unicode, never a SMuFL symbol ID.
            // An editing cursor left on the stock tempo-note glyph uses a
            // symbolic ScoreText fragment. Restore a textual font before IME
            // input so an entered word is not serialized as repeated noSym.
            const auto* text = interaction->editedText();
            if (auto* cursor = text->cursor(); cursor && cursor->format()->fontFamily() == u"ScoreText") {
                cursor->format()->setFontFamily(text->propertyDefault(mu::engraving::Pid::FONT_FACE).value<muse::String>());
            }
            QInputMethodEvent event;
            event.setCommitString(contents);
            interaction->editText(&event);
        } else if (op == "navigate") {
            using mu::notation::MoveDirection;
            using mu::engraving::Fraction;
            const auto* edited = interaction->editedText();
            const auto action = body.value("action").toString();
            const bool lyric = edited->isLyrics(), harmony = edited->isHarmony(), bass = edited->isFiguredBass();
            const auto direction = action == "previous" || action == "prev-beat" || action == "prev-measure" ? MoveDirection::Left : MoveDirection::Right;
            // Same routing as NotationActionController's text actions; the
            // web caret host only carries intent, never creates or relocates text.
            if (action == "next" || action == "previous" || action == "next-word") {
                const bool moveOnly = action != "next-word";
                if (lyric) interaction->navigateToLyrics(direction, moveOnly);
                else if (harmony) interaction->navigateToNearHarmony(direction, true);
                else if (bass) interaction->navigateToNearFiguredBass(direction);
                else {
                    require(edited->isFingering() || edited->isSticking(), "该文字不支持跳到下一位置");
                    interaction->navigateToNearText(direction);
                }
            } else if (action == "prev-beat" || action == "next-beat") {
                require(harmony, "只有和弦符号支持按拍移动");
                interaction->navigateToNearHarmony(direction, false);
            } else if (action == "prev-measure" || action == "next-measure") {
                require(harmony || bass, "measure navigation requires a chord symbol or figured bass");
                if (harmony) interaction->navigateToHarmonyInNearMeasure(direction);
                else interaction->navigateToFiguredBassInNearMeasure(direction);
            } else if (action.startsWith("fraction-")) {
                static const QMap<QString, Fraction> durations {
                    {"fraction-1",Fraction(1,64)},{"fraction-2",Fraction(1,32)},{"fraction-3",Fraction(1,16)},
                    {"fraction-4",Fraction(1,8)},{"fraction-5",Fraction(1,4)},{"fraction-6",Fraction(1,2)},
                    {"fraction-7",Fraction(1,1)},{"fraction-8",Fraction(2,1)},{"fraction-9",Fraction(4,1)}
                };
                require((harmony || bass) && durations.contains(action), "该位置不支持按指定时值移动");
                if (harmony) interaction->navigateToHarmony(durations.value(action));
                else interaction->navigateToFiguredBass(durations.value(action));
            } else {
                require(lyric, "只有歌词支持音节或歌词行操作");
                if (action == "next-syllable") interaction->navigateToNextSyllable();
                else if (action == "melisma") interaction->addMelisma();
                else if (action == "new-verse") interaction->addLyricsVerse();
                else if (action == "previous-verse" || action == "next-verse") {
                    interaction->navigateToLyricsVerse(action == "previous-verse" ? MoveDirection::Up : MoveDirection::Down);
                } else require(false,"不支持的文本导航");
            }
            if (interaction->isTextEditingStarted() && interaction->editedText() != edited) {
                remember(notation, interaction->editedText());
            }
        } else if (op == "select-all") {
            interaction->selectAll();
        } else if (op == "cursor-start" || op == "cursor-end") {
            auto* cursor = interaction->editedText()->cursor();
            require(bool(cursor), "原生文字光标不可用");
            if (op == "cursor-start") cursor->moveCursorToStart(); else cursor->moveCursorToEnd();
        } else if (op == "key") {
            const auto name = body.value("key").toString();
            const int key = name == "Backspace" ? Qt::Key_Backspace : name == "Delete" ? Qt::Key_Delete
                            : name == "ArrowLeft" ? Qt::Key_Left : name == "ArrowRight" ? Qt::Key_Right
                            : name == "ArrowUp" ? Qt::Key_Up : name == "ArrowDown" ? Qt::Key_Down
                            : name == "Home" ? Qt::Key_Home : name == "End" ? Qt::Key_End
                            : name == "Enter" ? Qt::Key_Return : 0;
            require(key != 0, "unsupported inline notation text key");
            Qt::KeyboardModifiers mods;
            if (body.value("extend").toBool()) mods |= Qt::ShiftModifier;
            if (body.value("control").toBool()) mods |= Qt::ControlModifier;
            QKeyEvent event(QEvent::KeyPress, key, mods);
            if (interaction->isEditAllowed(&event)) {
                interaction->editElement(&event);
            } else if (key == Qt::Key_Left || key == Qt::Key_Right) {
                const auto* edited = interaction->editedText();
                const bool left = key == Qt::Key_Left;
                // The native editor decides whether an arrow can move its
                // caret. Only when it declines, use its notation shortcut.
                QString action;
                if (mods == Qt::ControlModifier && (edited->isHarmony() || edited->isFiguredBass())) {
                    action = left ? QStringLiteral("prev-measure") : QStringLiteral("next-measure");
                } else if (mods == Qt::NoModifier && (edited->isLyrics() || edited->isFingering() || edited->isSticking())) {
                    action = left ? QStringLiteral("previous") : QStringLiteral("next");
                }
                if (!action.isEmpty()) return dispatch(notation, project, {{"operation", "navigate"}, {"action", action}});
            }
        } else if (op == "bold") interaction->toggleBold();
        else if (op == "italic") interaction->toggleItalic();
        else if (op == "underline") interaction->toggleUnderline();
        else if (op == "strike") interaction->toggleStrike();
        else if (op == "subscript") interaction->toggleSubScript();
        else if (op == "superscript") interaction->toggleSuperScript();
        else if (op == "finish") return finish(notation, project);
        else require(false, "不支持的原生文字操作");
        return false; // merge may hide metadata changes from the undo index; report them separately
    }

private:
    void remember(const INotationPtr& notation, const TextBase* text)
    {
        m_notation = notation;
        m_begin = notation->undoStack()->currentStateIndex();
        m_header = false;
        m_priorPlain.clear();
        if (text && headerStyle(text->textStyleType())
            && notation->elements()->msScore()->getText(text->textStyleType()) == text) {
            m_header = true;
            m_role = text->textStyleType();
            m_priorPlain = text->plainText().toQString();
        }
    }

    bool finish(const INotationPtr& notation, const INotationProjectPtr& project)
    {
        QInputMethodEvent clearPreedit;
        notation->interaction()->editText(&clearPreedit);
        notation->interaction()->endEditText();
        auto undo = notation->undoStack();
        bool metadataChanged = false;
        if (m_header && project && undo->currentStateIndex() > m_begin) {
            const auto* header = notation->elements()->msScore()->getText(m_role);
            const auto plain = header ? header->plainText().toQString() : QString();
            if (plain != m_priorPlain) {
                // Native project metadata is distinct from the visible text
                // frame. Align it only for a real top title/composer edit.
                auto meta = project->metaInfo();
                if (m_role == TextStyleType::TITLE && meta.title != plain) {
                    meta.title = plain;
                    metadataChanged = true;
                } else if (m_role == TextStyleType::COMPOSER && meta.composer != plain) {
                    meta.composer = plain;
                    metadataChanged = true;
                }
                if (metadataChanged) {
                    project->setMetaInfo(meta, true);
                }
            }
        }
        if (undo->currentStateIndex() > m_begin + 1) {
            // Native undo coalesces this completed edit/metadata into one entry.
            // No unrelated score mutations are accepted during this session.
            undo->mergeCommands(m_begin);
        }
        m_notation.reset();
        m_header = false;
        m_priorPlain.clear();
        return metadataChanged;
    }

    mu::notation::INotationWeakPtr m_notation;
    size_t m_begin = 0;
    bool m_header = false;
    TextStyleType m_role = TextStyleType::TITLE;
    QString m_priorPlain;
};
} // namespace mu::app::astra_rich_text
