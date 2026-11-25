// SPDX-License-Identifier: GPL-3.0-only
// Isolated, locally authored diagnostic glue for upstream MuseScore Studio v4.6.5.
// No alternate score model, layout engine, playback, or Astra UI is created here.
#pragma once

#include <cstring>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <map>

#include <QBuffer>
#include <QRegularExpression>
#include <thread>
#include <set>
#include <vector>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMimeData>
#include <QMap>
#include <QPainter>
#include <QSaveFile>
#include <QTransform>
#include <QtMath>

#define ASTRA_PANEL_EXPERIMENTAL_INSPECTOR 1
#include "astra_native_palette_bridge.h"
#include "astra_native_vector.h"
#include "astra_native_rich_text.h"
#include "astra_pointer_context.h"
#include "astra_native_saved_identity.h"
#include "engraving/dom/undo.h"
#include "astra_native_musicxml_export.h"
#include "draw/painter.h"
#include "global/io/buffer.h"
#include "draw/types/transform.h"
#include "modularity/ioc.h"
#include "notation/inotation.h"
#include "notation/inotationconfiguration.h"
#include "notation/inotationselection.h"
#include "engraving/dom/staff.h"
#include "engraving/dom/system.h"
#include "project/iprojectcreator.h"
#include "project/inotationwritersregister.h"
#include "project/inotationproject.h"
#include "context/iglobalcontext.h"
#include "engraving/dom/chord.h"
#include "engraving/dom/arpeggio.h"
#include "engraving/dom/barline.h"
#include "engraving/dom/layoutbreak.h"
#include "engraving/dom/clef.h"
#include "engraving/compat/dummyelement.h"
#include "engraving/dom/dynamic.h"
#include "engraving/dom/factory.h"
#include "engraving/dom/input.h"
#include "engraving/dom/hairpin.h"
#include "engraving/dom/ottava.h"
#include "engraving/dom/undo.h"
#include "engraving/dom/keysig.h"
#include "engraving/dom/measure.h"
#include "engraving/dom/note.h"
#include "engraving/types/typesconv.h"
#include "engraving/dom/page.h"
#include "engraving/dom/part.h"
#include "engraving/dom/sig.h"
#include "engraving/dom/tempo.h"
#include "engraving/dom/repeatlist.h"
#include "engraving/dom/system.h"
#include "engraving/compat/midi/pausemap.h"
#include "engraving/dom/score.h"
#include "engraving/dom/shadownote.h"
#include "engraving/rendering/iscorerenderer.h"
#include "engraving/dom/segment.h"
#include "engraving/dom/tuplet.h"
#include "engraving/dom/tie.h"
#include <array>
#include "engraving/dom/spanner.h"
#include "engraving/dom/tempotext.h"
#include "engraving/dom/timesig.h"
#include "commonscene/commonscenetypes.h"
#include "engraving/internal/qmimedataadapter.h"

namespace mu::app::astra_native_probe {

inline void check(bool condition, const char* detail)
{
    if (!condition) {
        throw std::runtime_error(detail);
    }
}

inline QByteArray sha256(const QString& path)
{
    QFile file(path);
    check(file.open(QIODevice::ReadOnly), "cannot read MIDI fixture");
    QCryptographicHash hash(QCryptographicHash::Sha256);
    check(hash.addData(&file), "cannot hash MIDI fixture");
    return hash.result().toHex();
}

struct ViewportScope {
    explicit ViewportScope(int page) : prior(mu::notation::astraScoreViewport()) { mu::notation::setAstraScoreViewport(page); }
    ~ViewportScope() { mu::notation::setAstraScoreViewport(prior); }
    int prior;
};
struct RangeMotionScope {
    explicit RangeMotionScope(bool enabled) { mu::notation::setAstraRangePreview(enabled); }
    ~RangeMotionScope() { mu::notation::setAstraRangePreview(false); }
};

class LiveScore final
{
public:
    LiveScore(const muse::modularity::ContextPtr& ctx, const QString& input) : m_ctx(ctx)
    {
        auto creator = muse::modularity::_ioc(ctx)->resolve<mu::project::IProjectCreator>("astra-native-probe");
        check(bool(creator), "project creator not initialized");
        m_project = creator->newProject(ctx);
        check(bool(m_project), "native project not created");
        const muse::Ret loaded = m_project->load(input);
        if (!loaded) {
            throw std::runtime_error(loaded.toString());
        }
        auto master = m_project->masterNotation();
        check(bool(master), "loaded project has no native master notation");
        m_notation = master->notation();
        check(bool(m_notation), "loaded project has no native notation");
        check(score()->parts().size() == 1 && score()->nstaves() == 2, "not a single grand-staff part");
        check(score()->parts().front()->instrumentId() == muse::String(u"piano"), "not the Piano instrument");
    }

    const mu::notation::INotationPtr& document() const { return m_notation; }
    const mu::project::INotationProjectPtr& project() const { return m_project; }

    mu::engraving::Score* score() const { return m_notation->elements()->msScore(); }

    void setMode(bool editable)
    {
        if (!editable && isInput()) {
            m_notation->interaction()->noteInput()->endNoteInput();
        }
        m_editable = editable;
    }

    bool isInput() const { return m_notation->interaction()->noteInput()->isNoteInputMode(); }
    size_t undoIndex() const { return m_notation->undoStack()->currentStateIndex(); }

    QJsonObject toolState() const
    {
        using namespace mu::engraving;
        const auto interaction = m_notation->interaction();
        const auto& state = interaction->noteInput()->state();
        const auto* item = interaction->selection()->element();
        const auto* chord = item && item->isNote() ? toNote(item)->chord() : nullptr;
        const auto* cr = chord ? static_cast<const ChordRest*>(chord)
                               : item && item->isChordRest() ? toChordRest(item) : nullptr;
        const auto duration = isInput() || !cr ? state.duration() : cr->durationType();
        QString name;
        switch (duration.type()) {
        case DurationType::V_LONG: name = "long"; break;
        case DurationType::V_BREVE: name = "breve"; break;
        case DurationType::V_128TH: name = "128"; break;
        case DurationType::V_256TH: name = "256"; break;
        case DurationType::V_512TH: name = "512"; break;
        case DurationType::V_1024TH: name = "1024"; break;
        case DurationType::V_WHOLE: name = "whole"; break;
        case DurationType::V_HALF: name = "half"; break;
        case DurationType::V_QUARTER: name = "quarter"; break;
        case DurationType::V_EIGHTH: name = "eighth"; break;
        case DurationType::V_16TH: name = "sixteenth"; break;
        case DurationType::V_32ND: name = "thirtysecond"; break;
        case DurationType::V_64TH: name = "sixtyfourth"; break;
        default: break;
        }
        const auto accidentalType = isInput() ? state.accidentalType()
                                    : item && item->isNote() ? toNote(item)->accidentalType() : AccidentalType::NONE;
        const QString accidental = accidentalType == AccidentalType::SHARP ? "sharp"
                                   : accidentalType == AccidentalType::FLAT ? "flat"
                                   : accidentalType == AccidentalType::NATURAL ? "natural"
                                   : accidentalType == AccidentalType::FLAT2 ? "flat2"
                                   : accidentalType == AccidentalType::SHARP2 ? "sharp2" : "";
        using mu::notation::NoteInputMethod;
        const auto mode = state.noteEntryMethod();
        const auto* modeId = mode == NoteInputMethod::BY_DURATION ? "duration"
                           : mode == NoteInputMethod::RHYTHM ? "rhythm"
                           : mode == NoteInputMethod::REPITCH ? "repitch"
                           : mode == NoteInputMethod::TIMEWISE ? "timewise" : "note-name";
        return {{"input", isInput()},
                {"input-method", isInput() ? modeId : ""},
                {"duration", name},
                {"dot", duration.dots() > 0}, {"dotCount", duration.dots()},
                {"logicalStaff", cr ? int(cr->staffIdx()) : -1},
                {"displayStaff", cr ? int(cr->vStaffIdx()) : -1},
                {"chordNotes", chord ? int(chord->notes().size()) : 0}, {"rest", isInput() ? state.rest() : item && item->isRest()},
                {"voice", 1 + int(isInput() || !item ? state.voice() : item->voice())},
                {"accidental", accidental}, {"tie", item && item->isNote() && toNote(item)->tieFor()},
                {"triplet", cr && cr->tuplet()},
                {"selectedRhythm", bool(cr)}, {"canPitch", !interaction->selection()->notes().empty()},
                {"selection", selectionState()},
                {"gripEdit", hasSupportedGrip()}};
    }

    QJsonObject selectionState() const
    {
        const auto selection = m_notation->interaction()->selection();
        const auto& elements = selection->elements();
        const bool rhythm = std::any_of(elements.cbegin(), elements.cend(),
                                        [](const auto* e) { return e && (e->isChordRest() || e->isNote()); });
        QJsonObject result{{"mode", selection->isNone() ? "none" : selection->isRange() ? "range"
                             : selection->elements().size() > 1 ? "list" : "single"},
                           {"count", int(selection->elements().size())}, {"noteCount", int(selection->notes().size())}};
        result.insert("hasRhythm",rhythm);
        if (selection->isRange()) {
            const auto r = selection->range();
            const auto f = [](const mu::engraving::Fraction& t) {
                return QJsonArray{t.numerator(), t.denominator()};
            };
            result.insert("from", f(r->startTick()));
            result.insert("to", f(r->endTick()));
            result.insert("staffFrom", int(r->startStaffIndex()));
            result.insert("staffTo", int(r->endStaffIndex()));
        }
        return result;
    }

    void setView(const QTransform& matrix)
    {
        m_notation->viewState()->setMatrix(muse::draw::Transform::fromQTransform(matrix), nullptr);
        m_notation->viewState()->setMatrixInited(true);
    }

    QTransform view() const
    {
        return muse::draw::Transform::toQTransform(m_notation->viewState()->matrix());
    }

    mu::engraving::Note* fixtureAnchor() const
    {
        using namespace mu::engraving;
        auto* measure = score()->firstMeasure();
        check(measure != nullptr, "first measure missing");
        auto* segment = measure->first(SegmentType::ChordRest);
        check(segment != nullptr && segment->tick().isZero(), "fixture first beat missing");
        auto* item = segment->element(0);
        check(item && item->isChord(), "fixture first chord missing");
        auto* chord = toChord(item);
        check(chord->notes().size() == 1, "fixture anchor has unexpected chord size");
        return chord->notes().front();
    }

    int pageIndex() const { return m_page; }
    int pageCount() const { return m_notation->painting()->pageCount(); }

    muse::RectF pageRect() const
    {
        const auto& pages = m_notation->elements()->pages();
        check(m_page >= 0 && size_t(m_page) < pages.size(), "native engraving did not produce the page");
        const auto* page = pages.at(size_t(m_page));
        return page->ldata()->bbox().translated(page->pos());
    }

    QTransform workerViewport() const
    {
        const auto page = pageRect();
        check(std::isfinite(page.width()) && page.width() > 0, "invalid native page width");
        const double scale = 920.0 / page.width();
        QTransform view;
        view.translate(20.0 - scale * page.x(), -scale * page.y());
        view.scale(scale, scale);
        return view;
    }

    void setPage(int to)
    {
        check(to >= 0 && to < pageCount(), "没有此谱面页");
        m_page = to;
        setView(workerViewport());
    }

    // Repaint only a canvas region onto the previous frame (same page, size and
    // scale). Pixels inside the device-aligned clip are drawn exactly as a full
    // paint draws them; everything outside is the unchanged previous frame.
    QImage paintRegion(double rasterScale, const QImage& previous, const QRectF& canvasRect) const
    {
        ViewportScope viewport(m_page);
        QImage image = previous.copy();
        image.setDevicePixelRatio(rasterScale);
        const QRectF logical = view().mapRect(canvasRect);
        const QRect device = QRectF(logical.x()*rasterScale, logical.y()*rasterScale,
                                    logical.width()*rasterScale, logical.height()*rasterScale)
                                 .toAlignedRect().intersected(QRect(QPoint(0,0), image.size()));
        if (device.isEmpty()) return image;
        const QRectF clip = QRectF(device.x()/rasterScale, device.y()/rasterScale, device.width()/rasterScale, device.height()/rasterScale)
                                .intersected(view().mapRect(pageRect().toQRectF()).adjusted(-2, -2, 2, 2));
        if (clip.isEmpty()) return image;
        QPainter qt(&image);
        qt.setRenderHint(QPainter::Antialiasing);
        qt.setClipRect(clip);
        qt.fillRect(clip, QColor(240, 241, 247));
        muse::draw::Painter native(&qt, "Astra native score canvas proof");
        native.setWorldTransform(muse::draw::Transform::fromQTransform(view()));
        const auto logicalClip = muse::RectF::fromQRectF(view().inverted().mapRect(clip));
        m_notation->painting()->paintView(&native, logicalClip, !editable());
        qt.end();
        return image;
    }

    // The page as a vector display list: the same paintView call as paint(),
    // into a recording device instead of an image (see astra_native_vector.h).
    QJsonObject vectorPage() const
    {
        ViewportScope viewport(m_page);
        const auto page = pageRect();
        const int height = qBound(480, qCeil(920.0 * page.height() / page.width()), 1800);
        const QSize size(960, height);
        astra_vector::Device device(size);
        QPainter qt(&device);
        qt.setRenderHint(QPainter::Antialiasing);
        const QRectF clip = QRectF(QPointF(0, 0), QSizeF(size))
                                .intersected(view().mapRect(page.toQRectF()).adjusted(-2, -2, 2, 2));
        muse::draw::Painter native(&qt, "Astra native score vector");
        native.setWorldTransform(muse::draw::Transform::fromQTransform(view()));
        const auto logicalClip = muse::RectF::fromQRectF(view().inverted().mapRect(clip));
        m_notation->painting()->paintView(&native, logicalClip, !editable());
        if (isInput()) {
            const auto cursor = m_notation->interaction()->noteInput()->cursorRect();
            if (cursor.isValid()) {
                qt.setWorldTransform(view());
                qt.setPen(Qt::NoPen);
                qt.setBrush(QColor(54, 119, 222, 75));
                qt.drawRect(cursor.toQRectF());
            }
        }
        qt.end();
        return device.engine().result(QSizeF(size), clip);
    }

    QRectF takePointerDamage() { const auto r = m_pointerDamage; m_pointerDamage = QRectF(); return r; }

    QImage paint(double rasterScale = 1.0) const
    {
        ViewportScope viewport(m_page);
        const auto page = pageRect();
        const int height = qBound(480, qCeil(920.0 * page.height() / page.width()), 1800);
        const QSize size(960, height);
        QImage image(QSize(qRound(size.width()*rasterScale),qRound(size.height()*rasterScale)), QImage::Format_ARGB32_Premultiplied);
        image.setDevicePixelRatio(rasterScale);
        image.fill(QColor(240, 241, 247));
        QPainter qt(&image);
        qt.setRenderHint(QPainter::Antialiasing);
        // Only this page: MuseScore lays pages side by side, and the viewport's
        // margin would otherwise show a sliver of the next page. Keep 2 units
        // around the page for its own border.
        const QRectF clip = QRectF(QPointF(0, 0), QSizeF(size))
                                .intersected(view().mapRect(page.toQRectF()).adjusted(-2, -2, 2, 2));
        qt.setClipRect(clip);
        muse::draw::Painter native(&qt, "Astra native score canvas proof");
        native.setWorldTransform(muse::draw::Transform::fromQTransform(view()));
        const auto logicalClip = muse::RectF::fromQRectF(view().inverted().mapRect(clip));
        // AbstractNotationPaintView::paint: isPrinting = publishMode() || readonly().
        m_notation->painting()->paintView(&native, logicalClip, !editable());
        if (isInput()) {
            // Only the upstream input cursor is passed to the presentation
            // layer, with the same native page transform used for picking.
            const auto cursor = m_notation->interaction()->noteInput()->cursorRect();
            if (cursor.isValid()) {
                qt.setWorldTransform(view());
                qt.setPen(Qt::NoPen);
                qt.setBrush(QColor(54, 119, 222, 75));
                qt.drawRect(cursor.toQRectF());
            }
        }
        qt.end();
        return image;
    }

    QJsonObject shadowAt(const QPointF& pixel, double rasterScale)
    {
        auto interaction = m_notation->interaction();
        const auto page = pageRect();
        const int height = qBound(480, qCeil(920.0 * page.height() / page.width()), 1800);
        if (!isInput() || pixel.x() < 0 || pixel.x() >= 960 || pixel.y() < 0 || pixel.y() >= height) return {};
        const bool visible = interaction->showShadowNote(muse::PointF::fromQPointF(view().inverted().map(pixel)));
        struct Hide { const mu::notation::INotationInteractionPtr& i; ~Hide(){ i->hideShadowNote(); } } hide{interaction};
        if (!visible) return {};
        const auto logical = interaction->shadowNoteRect();
        if (!logical.isValid()) return {};
        const auto rect = view().mapRect(logical.toQRectF()).toAlignedRect().adjusted(-2,-2,2,2)
                         .intersected(QRect(0,0,960,height));
        if (rect.isEmpty() || rect.width() > 192 || rect.height() > 192) return {};
        QImage image(QSize(qRound(rect.width()*rasterScale), qRound(rect.height()*rasterScale)), QImage::Format_ARGB32_Premultiplied);
        image.setDevicePixelRatio(rasterScale);
        image.fill(Qt::transparent);
        QPainter qt(&image);
        qt.setRenderHint(QPainter::Antialiasing);
        muse::draw::Painter paint(&qt,"Astra native shadow note");
        QTransform transform;
        transform.translate(-rect.x(), -rect.y());
        paint.setWorldTransform(muse::draw::Transform::fromQTransform(view() * transform));
        score()->renderer()->drawItem(interaction->shadowNote(), &paint);
        qt.end();
        QByteArray png;
        QBuffer buffer(&png);
        if (!buffer.open(QIODevice::WriteOnly) || !image.save(&buffer,"PNG") || png.size() > 32*1024) return {};
        return {{"shadowPng",QString::fromLatin1(png.toBase64())},
                {"shadowRect",QJsonArray{rect.x(),rect.y(),rect.width(),rect.height()}},
                {"shadowScale",rasterScale}};
    }

    QByteArray playbackSnapshot() const
    {
        using namespace mu::project;
        auto registry = muse::modularity::_ioc(m_ctx)->resolve<INotationWritersRegister>("astra-native-playback");
        check(bool(registry), "missing native notation writer registry");
        auto writer = registry->writer("mid");
        check(bool(writer), "missing native MIDI writer");
        muse::io::Buffer buffer;
        check(buffer.open(muse::io::IODevice::WriteOnly), "cannot buffer native playback");
        const INotationWriter::Options options = {
            { INotationWriter::OptionKey::UNIT_TYPE, muse::Val(INotationWriter::UnitType::PER_PART) }
        };
        check(bool(writer->write(m_notation, buffer, options)), "native MIDI projection failed");
        const auto content = buffer.data();
        // Fit a single existing 1 MiB channel response. Refuse large scores
        // instead of cutting their final events or taking a stale disk path.
        check(content.size() > 14 && content.size() <= 700 * 1024, "native MIDI projection exceeds channel budget");
        return QByteArray(content.constChar(), qsizetype(content.size()));
    }

    QJsonObject playbackSelection() const
    {
        const auto selection = m_notation->interaction()->selection();
        if (selection->isRange()) {
            return {{"tick", selection->range()->startTick().ticks()}, {"endTick", selection->range()->endTick().ticks()}, {"kind", "range"}};
        }
        const auto* item = selection->element();
        if (item && (item->isNote() || item->isChordRest())) {
            return {{"tick", item->tick().ticks()}, {"endTick", (item->tick() + score()->inputState().ticks()).ticks()}, {"kind", "element"}};
        }
        // MuseScore (resolveStartPlayableElement) starts from any selected
        // element when paused; while playing only seekAllowed types seek.
        if (item && item->tick().ticks() >= 0) {
            const bool playing = item->isMeasure() || item->isBarLine() || item->isMMRest();
            return {{"tick", item->tick().ticks()}, {"endTick", item->tick().ticks()}, {"kind", playing ? "element" : "other"}};
        }
        return {};
    }

    QJsonObject playbackCursorLayout() const
    {
        using namespace mu::engraving;
        const auto& pages = m_notation->elements()->pages();
        const auto* page = pages.at(size_t(pageIndex()));
        const auto viewport = workerViewport();
        const auto* sc = score();
        const double gap = sc->style().spatium();
        QJsonArray rows;
        for (const auto* system : page->systems()) {
            const double staffY = system->staffYpage(0) + page->pos().y();
            double bottom = 0.0;
            for (size_t i=0; i<sc->nstaves(); ++i) {
                auto* staff = system->staff(i);
                if (staff && staff->show() && sc->staff(i)->show()) bottom = staff->bbox().bottom();
            }
            const double y=viewport.map(QPointF(0.0,staffY-3*gap)).y();
            const double w=8*viewport.m11(), h=(6*gap+bottom)*viewport.m22();
            for (const auto* base : system->measures()) {
                if (!base->isMeasure()) continue;
                const auto* measure=toMeasure(base);
                for (auto* seg=measure->first(SegmentType::ChordRest); seg;) {
                    const auto t0=seg->tick();
                    const auto x0=int(seg->canvasPos().x())-gap;
                    auto* next=seg->next(SegmentType::ChordRest);
                    while (next && !next->visible()) next=next->next(SegmentType::ChordRest);
                    const auto t1=next ? next->tick() : measure->endTick();
                    double x1=0.0;
                    if (next) x1=next->canvasPos().x();
                    else if (auto* stroke=measure->findSegment(SegmentType::EndBarLine,t1)) x1=stroke->canvasPos().x();
                    else x1=measure->canvasPos().x()+measure->width();
                    if (t0<t1 && sc->tick2measureMM(t0)==measure) {
                        const double px0=viewport.map(QPointF(x0,0.0)).x();
                        const double px1=viewport.map(QPointF(x1-gap,0.0)).x();
                        check(std::isfinite(px0) && std::isfinite(px1) && std::isfinite(y) && w>0 && h>0,
                              "invalid native cursor geometry");
                        check(rows.size()<4096, "native page cursor geometry exceeds budget");
                        rows.append(QJsonArray{t0.ticks(),t1.ticks(),px0,px1,y,w,h});
                    }
                    seg=next;
                }
            }
        }
        return {{"page",pageIndex()+1},{"rows",rows}};
    }

    QJsonObject playbackBeatGeometry() const
    {
        using namespace mu::engraving;
        const auto* sc=score();
        const auto* sig=sc->sigmap();
        QJsonArray rows;
        if (sig->size()>4096) return {};
        for (const auto& entry : *sig) {
            const auto actual=entry.second.timesig();
            rows.append(QJsonArray{entry.first,entry.second.bar(),actual.numerator(),ticks_beat(actual.denominator())});
        }
        return {{"rows",rows},{"maxMeasure",int(const_cast<Score*>(sc)->measures()->size())}};
    }

    QJsonObject playbackBarMap() const
    {
        // The pinned MIDI writer renders the same RepeatList and applies this
        // PauseMap to both note events and tempo. This is not Notes geometry.
        auto* s = score();
        const auto& repeats = s->repeatList();
        check(!repeats.empty(), "piano score has no played measures");
        mu::engraving::PauseMap pauses;
        pauses.calculate(s);
        std::map<const mu::engraving::Measure*, int> ordinals;
        int ordinal = 0;
        for (auto* m = s->firstMeasure(); m; m = m->nextMeasure()) {
            ordinals[m] = ordinal++;
        }
        const auto& pages = m_notation->elements()->pages();
        QJsonArray bars, spans, pauseOffsets, clickGeometry;
        check(repeats.size() <= 12000, "native playback repeat map exceeds bounds");
        const auto* last = repeats.back();
        const int playedEnd = last->utick + last->len();
        const auto offsetAt = [&](int tick) { return pauses.tickWithPauses(tick) - tick; };
        // PauseMap's public API is authoritative; the native storage is private
        // in the worker build. Offsets monotonically increase by inserted ticks.
        int p = 0, offset = offsetAt(0);
        const int finalOffset = offsetAt(playedEnd);
        pauseOffsets.append(QJsonArray{p, offset});
        while (offset != finalOffset) {
            check(pauseOffsets.size() < 12000 && p < playedEnd, "native playback pause map exceeds bounds");
            auto lo = p + 1;
            auto hi = playedEnd;
            while (lo < hi) {
                const int mid = lo + (hi - lo) / 2;
                if (offsetAt(mid) > offset) hi = mid;
                else lo = mid + 1;
            }
            p = lo;
            const int nextOffset = offsetAt(p);
            check(nextOffset > offset, "invalid native playback pause offset");
            offset = nextOffset;
            pauseOffsets.append(QJsonArray{p, offset});
        }
        int previous = -1;
        for (const auto* repeat : repeats) {
            spans.append(QJsonArray{repeat->tick, repeat->endTick(), repeat->utick});
            for (const auto* m : repeat->measureList()) {
                const int tick = m->tick().ticks();
                if (tick < repeat->tick || tick >= repeat->endTick()) {
                    continue;
                }
                const int midiTick = pauses.tickWithPauses(repeat->utick + tick - repeat->tick);
                if (midiTick <= previous) {
                    continue;
                }
                previous = midiTick;
                check(bars.size() < 12000, "too many played piano measures");
                // Publish the target in the same 960px source geometry used by paint().
                // Some irregular/native layouts need no visual target; never guess.
                const auto* sys = m->system();
                const auto* layoutPage = sys ? sys->page() : nullptr;
                const auto page = std::find(pages.begin(), pages.end(), layoutPage);
                int number = 0, y = 0;
                if (page != pages.end()) {
                    const auto viewRect = layoutPage->ldata()->bbox().translated(layoutPage->pos());
                    const double ty = (sys->canvasPos().y() - viewRect.y()) * 920.0 / viewRect.width();
                    if (std::isfinite(ty) && ty >= 0 && ty < 1800) {
                        number = int(std::distance(pages.begin(),page)) + 1;
                        y = qRound(ty);
                    }
                }
                bars.append(QJsonArray{ordinals.at(m), midiTick, number, y});
                const auto nominal = s->sigmap()->timesig(tick).nominal();
                const double bps = s->tempomap()->multipliedTempo(tick).val;
                const double meterBpm = nominal.tempo2beatsPerMinute(bps);
                int barIdx=0, beatIdx=0, tickWithinBeat=0;
                s->sigmap()->tickValues(tick,&barIdx,&beatIdx,&tickWithinBeat);
                const auto beatUnit = mu::engraving::ticks_beat(s->sigmap()->timesig(tick).timesig().denominator());
                clickGeometry.append(QJsonArray{repeat->utick + tick - repeat->tick,
                    std::min(m->endTick().ticks(), repeat->endTick())-tick, nominal.dUnitTicks(),
                    nominal.beatTicks(), nominal.ticksPerMeasure(), m->isAnacrusis(), std::isfinite(meterBpm) && meterBpm > 0 ? 60.0 / meterBpm : 0., beatUnit, tickWithinBeat});
            }
        }
        return {{"bars", bars}, {"endMidiTick", pauses.tickWithPauses(playedEnd)},
                {"spans", spans}, {"pauses", pauseOffsets}, {"selection", playbackSelection()},
                {"writtenBounds", QJsonArray{s->firstMeasure()->tick().ticks(), s->lastMeasure()->endTick().ticks()}},
                {"clickGeometry", clickGeometry}, {"beatGeometry", playbackBeatGeometry()}};
    }

    void exportPdf(const QString& output)
    {
        using namespace mu::project;
        auto registry = muse::modularity::_ioc(m_ctx)->resolve<INotationWritersRegister>("astra-native-pdf");
        check(bool(registry), "missing native notation writer registry");
        auto writer = registry->writer("pdf");
        check(bool(writer), "missing native PDF writer");
        muse::io::Buffer buffer;
        check(buffer.open(muse::io::IODevice::WriteOnly), "cannot buffer native PDF");
        const INotationWriter::Options options = {
            { INotationWriter::OptionKey::UNIT_TYPE, muse::Val(INotationWriter::UnitType::PER_PART) }
        };
        check(bool(writer->write(m_notation, buffer, options)), "native PDF writer failed");
        const auto& content = buffer.data();
        check(content.size() > 12 && content.size() < 64 * 1024 * 1024, "invalid native PDF output");
        QSaveFile file(output);
        check(file.open(QIODevice::WriteOnly)
              && file.write(content.constChar(), qint64(content.size())) == qint64(content.size()) && file.commit(),
              "无法原子写入 PDF");
    }

    bool selectAt(const QPointF& widgetPoint, mu::engraving::Note* expected = nullptr)
    {
        if (!m_editable) {
            return false;
        }
        const auto p = view().inverted().map(widgetPoint);
        auto hitList = m_notation->interaction()->hitElements(muse::PointF(p.x(), p.y()), 5.f);
        auto hit = std::find_if(hitList.rbegin(), hitList.rend(), [expected](mu::engraving::EngravingItem* item) {
            return expected ? item == expected : item->isNote() || item->isRest() || item->isBarLine();
        });
        if (hit == hitList.rend()) {
            m_notation->interaction()->clearSelection();
            return false;
        }
        m_notation->interaction()->select({*hit}, mu::notation::SelectType::SINGLE);
        return true;
    }

    bool hasSupportedGrip() const
    {
        const auto interaction = m_notation->interaction();
        const auto* item = interaction->selection()->element();
        return item && item->isSpannerSegment() && item->hasGrips() && interaction->isGripEditStarted();
    }

    float pickWidth(double cssScale) const
    {
        // The configured neighborhood in stock Qt is a physical canvas
        // tolerance (half the value on each side), not a fixed number of
        // logical score units. Account for raster scale and WebEngine zoom.
        if (!std::isfinite(cssScale) || cssScale < 0.10 || cssScale > 10.0 || view().m11() <= 0.0) return 5.f;
        const auto config = muse::modularity::_ioc(m_ctx)->resolve<mu::notation::INotationConfiguration>("astra-native-pointer");
        return config ? float(config->selectionProximity() * 0.5 / (view().m11() * cssScale)) : 5.f;
    }

    void click(const QPointF& point, bool shift = false, bool control = false, bool armGrip = true, double cssScale = 0.0)
    {
        check(m_editable, "请先开启编辑模式");
        ViewportScope viewport(m_page);
        const auto p = view().inverted().map(point);
        const muse::PointF logical(p.x(), p.y());
        const auto interaction = m_notation->interaction();
        using namespace mu::notation;
        if (!isInput()) {
            // Ctrl+Shift outgoing drag is a separate upstream gesture, not a pinch range.
            if (shift && control) {
                return;
            }
            if (armGrip && hasSupportedGrip() && interaction->isHitGrip(logical)) {
                interaction->startEditGrip(logical);
                return;
            }
            auto hits = interaction->hitElements(logical, pickWidth(cssScale));
            if (hits.empty()) {
                if (!shift && !control) {
                    interaction->clearSelection();
                }
                return;
            }
            auto* staff = interaction->hitStaff(logical);
            const auto staffIndex = staff ? staff->idx() : muse::nidx;
            auto* hit = hits.back();
            interaction->setHitElementContext({hit, staff});
            if (control && hits.size() > 1) {
                // Pinned NotationViewInputController::cycleOverlappingHitElements.
                // Don't convert a continuous range to a list before asking the
                // native ADD selection primitive, and don't invent a same-tick
                // filter: users may need to cycle a note and a different symbol.
                const size_t n = hits.size();
                size_t next = n - 1;
                mu::engraving::EngravingItem* candidate = hits[next];
                std::vector<mu::engraving::EngravingItem*> selected;
                bool sawSelected = false;
                for (size_t i = 0; i <= n; ++i) {
                    if (sawSelected) {
                        candidate = hits[next];
                        if (!candidate->isMeasure()) break;
                    } else if (hits[next]->selected()) {
                        sawSelected = true;
                        selected.push_back(hits[next]);
                        candidate = nullptr;
                    }
                    next = (next + 1) % n;
                }
                if (candidate && !candidate->selected()) {
                    for (auto* item : selected) item->score()->deselect(item);
                    interaction->select({candidate}, SelectType::ADD, staffIndex);
                }
                return;
            }
            if (hit->isMeasure() && staffIndex == muse::nidx) {
                if (!shift && !control) {
                    interaction->clearSelection();
                }
                return;
            }
            interaction->select({hit}, shift ? SelectType::RANGE : control ? SelectType::ADD
                                                 : SelectType::SINGLE, staffIndex);
            if (armGrip && !shift && !control && interaction->selection()->element() == hit &&
                hit->isSpannerSegment() && hit->hasGrips() && hit->needStartEditingAfterSelecting()) {
                interaction->startEditElement(hit);
            }
            return;
        }
        const auto result = interaction->noteInput()->putNote(logical, shift, control);
        if (!result) {
            throw std::runtime_error(result.toString());
        }
    }

    // ---- MuseScore Layout panel: the parts a single-piano score uses ----
    // Read: the instrument (name, abbreviation, hide-empty settings) and its
    // staves (visible, small, hide when empty, show if the system is empty).
    QJsonObject layoutTree() const
    {
        using namespace mu::engraving;
        const auto parts = m_notation->parts();
        QJsonArray out;
        for (const Part* part : parts->partList()) {
            QJsonArray staves;
            int index = 0;
            for (const Staff* staff : parts->staffList(part->id())) {
                const auto config = parts->staffConfig(staff->id());
                staves.append(QJsonObject{
                    {"id", QString::fromStdString(staff->id().toStdString())}, {"index", ++index},
                    {"visible", staff->show()}, {"small", config.staffType.isSmall()},
                    {"hideWhenEmpty", int(staff->hideWhenEmpty())},
                    {"showIfEntireSystemEmpty", staff->showIfEntireSystemEmpty()}});
            }
            out.append(QJsonObject{
                {"id", QString::fromStdString(part->id().toStdString())},
                {"name", part->instrument()->nameAsPlainText().toQString()},
                {"abbreviation", part->instrument()->abbreviatureAsPlainText().toQString()},
                {"visible", part->show()}, {"hideWhenEmpty", int(part->hideWhenEmpty())},
                {"hideStavesWhenIndividuallyEmpty", part->hideStavesWhenIndividuallyEmpty()},
                {"staves", staves}});
        }
        return QJsonObject{{"parts", out}};
    }

    // Write exactly as MuseScore's InstrumentSettingsModel / StaffSettingsModel
    // and the Layout panel's visibility buttons do (all undoable).
    void layoutSet(const QJsonObject& body)
    {
        using namespace mu::engraving;
        check(editable(), "Edit mode required");
        const auto parts = m_notation->parts();
        const auto field = body.value("field").toString();
        const auto value = body.value("value");
        const muse::ID id(body.value("id").toString().toStdString());
        const bool autoOnOff = field == "hideWhenEmpty" && value.isDouble() && value.toInt() >= 0 && value.toInt() <= 2;
        if (body.value("target").toString() == "part") {
            const Part* part = parts->part(id);
            check(part, "no such instrument");
            mu::notation::InstrumentKey key;
            key.partId = part->id();
            key.instrumentId = part->instrumentId();
            key.tick = Part::MAIN_INSTRUMENT_TICK;
            if (field == "visible" && value.isBool()) {
                parts->setPartVisible(part->id(), value.toBool());
            } else if (field == "name" && value.isString() && value.toString().size() <= 200) {
                parts->setInstrumentName(key, value.toString());
            } else if (field == "abbreviation" && value.isString() && value.toString().size() <= 200) {
                parts->setInstrumentAbbreviature(key, value.toString());
            } else if (autoOnOff || (field == "hideStavesWhenIndividuallyEmpty" && value.isBool())) {
                m_notation->undoStack()->prepareChanges(muse::TranslatableString("undoableAction", "Change instrument settings"));
                auto* mutablePart = const_cast<Part*>(part);
                if (autoOnOff) {
                    mutablePart->undoChangeProperty(Pid::HIDE_WHEN_EMPTY, PropertyValue(static_cast<AutoOnOff>(value.toInt())));
                } else {
                    mutablePart->undoChangeProperty(Pid::HIDE_STAVES_WHEN_INDIVIDUALLY_EMPTY, PropertyValue(value.toBool()));
                }
                m_notation->undoStack()->commitChanges();
            } else {
                check(false, "invalid instrument setting");
            }
        } else {
            const Staff* staff = parts->staff(id);
            check(staff, "no such staff");
            if (field == "visible" && value.isBool()) {
                parts->setStaffVisible(staff->id(), value.toBool());
            } else if (field == "small" && value.isBool()) {
                auto config = parts->staffConfig(staff->id());
                config.staffType.setSmall(value.toBool());
                parts->setStaffConfig(staff->id(), config);
            } else if (autoOnOff || (field == "showIfEntireSystemEmpty" && value.isBool())) {
                m_notation->undoStack()->prepareChanges(muse::TranslatableString("undoableAction", "Change staff settings"));
                auto* mutableStaff = const_cast<Staff*>(staff);
                if (autoOnOff) {
                    mutableStaff->undoChangeProperty(Pid::HIDE_WHEN_EMPTY, PropertyValue(static_cast<AutoOnOff>(value.toInt())));
                } else {
                    mutableStaff->undoChangeProperty(Pid::SHOW_IF_ENTIRE_SYSTEM_EMPTY, PropertyValue(value.toBool()));
                }
                m_notation->undoStack()->commitChanges();
            } else {
                check(false, "invalid staff setting");
            }
        }
        score()->update();
    }

    // "Tidy score" (one undoable step): a title / composer in the top frame
    // when missing (Score::addText, as MuseScore adds frame text), and tempo
    // markings a person can read. Tempo marks keep their exact playback value
    // (TEMPO_FOLLOW_TEXT off before the label changes); the opening tempo and
    // real changes (>= 4%) show as whole numbers, the rest are hidden.
    QJsonObject tidyScore(const QString& title, const QString& composer)
    {
        using namespace mu::engraving;
        check(editable(), "Edit mode required");
        auto* sc = score();
        int titled = 0, rounded = 0, hidden = 0;
        m_notation->undoStack()->prepareChanges(muse::TranslatableString("undoableAction", "Tidy score"));
        const auto hasText = [&](TextStyleType type) {
            const MeasureBase* frame = sc->first();
            if (!frame || !frame->isVBox()) return false;
            for (const EngravingItem* e : frame->el())
                if (e && e->isTextBase() && toTextBase(e)->textStyleType() == type) return true;
            return false;
        };
        const auto addFrameText = [&](TextStyleType type, const QString& text) {
            if (text.trimmed().isEmpty() || hasText(type)) return;
            if (auto* t = sc->addText(type)) {
                t->undoChangeProperty(Pid::TEXT, PropertyValue(muse::String::fromQString(text.trimmed())));
                ++titled;
            }
        };
        addFrameText(TextStyleType::TITLE, title);
        addFrameText(TextStyleType::COMPOSER, composer);
        static const QRegularExpression number(QStringLiteral("=\\s*(\\d+(?:\\.\\d+)?)"));
        double shown = -1;
        for (Segment* seg = sc->firstSegment(SegmentType::ChordRest); seg; seg = seg->next1(SegmentType::ChordRest)) {
            for (EngravingItem* e : seg->annotations()) {
                if (!e || !e->isTempoText()) continue;
                TempoText* tempo = toTempoText(e);
                QString text = tempo->xmlText().toQString();
                const auto match = number.match(text);
                if (!match.hasMatch()) continue;
                const double value = match.captured(1).toDouble();
                const long whole = std::lround(value);
                const bool change = shown < 0 || std::abs(double(whole) - shown) >= 0.04 * shown;
                if (change) {
                    if (match.captured(1) != QString::number(whole)) {
                        if (tempo->followText()) static_cast<EngravingItem*>(tempo)->undoChangeProperty(Pid::TEMPO_FOLLOW_TEXT, PropertyValue(false), PropertyFlags::NOSTYLE);
                        text.replace(match.capturedStart(1), match.capturedLength(1), QString::number(whole));
                        static_cast<EngravingItem*>(tempo)->undoChangeProperty(Pid::TEXT, PropertyValue(muse::String::fromQString(text)), PropertyFlags::NOSTYLE);
                        ++rounded;
                    }
                    shown = double(whole);
                } else if (tempo->visible()) {
                    static_cast<EngravingItem*>(tempo)->undoChangeProperty(Pid::VISIBLE, PropertyValue(false), PropertyFlags::NOSTYLE);
                    ++hidden;
                }
            }
        }
        m_notation->undoStack()->commitChanges();
        sc->update();
        return QJsonObject{{"tidyTitled", titled}, {"tidyRounded", rounded}, {"tidyHidden", hidden}};
    }

    int tickAt(const QPointF& point, double cssScale = 0.) const
    {
        // Read only (no selection): the written tick of the note, chord or
        // rest under the pointer, as a click there would select it.
        using namespace mu::engraving;
        const auto p = view().inverted().map(point);
        const auto* hit = m_notation->interaction()->hitElement(muse::PointF(p.x(),p.y()), pickWidth(cssScale));
        for (const EngravingItem* e = hit; e; e = e->parentItem()) {
            if (e->isNote()) return toNote(e)->chord()->tick().ticks();
            if (e->isChordRest()) return toChordRest(e)->tick().ticks();
            if (e->isMeasure() || e->isSystem() || e->isPage()) break;
        }
        return -1;
    }

    bool canPanAt(const QPointF& point, double cssScale = 0.) const
    {
        // Read only: mirror the plain-drag canvas fallback in upstream
        // NotationViewInputController::mouseMoveEvent without selecting.
        const auto interaction = m_notation->interaction();
        if (isInput() || interaction->isTextEditingStarted() || interaction->isGripEditStarted()) return false;
        if (!editable()) return true;
        const auto p = view().inverted().map(point);
        const auto* hit = interaction->hitElement(muse::PointF(p.x(),p.y()), pickWidth(cssScale));
        return !hit || (!hit->isMovable() && !interaction->isElementEditStarted());
    }

    QString contextClick(const QPointF& point, bool& canRetainRaster, double cssScale = 0.)
    {
        using namespace mu::notation;
        check(editable(), "context requires edit mode");
        const auto p = view().inverted().map(point);
        const muse::PointF logical(p.x(), p.y());
        const auto interaction = m_notation->interaction();
        if (isInput()) {
            interaction->noteInput()->removeNote(logical);
            return {};
        }
        auto* item = interaction->hitElement(logical, pickWidth(cssScale));
        auto* staff = interaction->hitStaff(logical);
        const auto staffIndex = staff ? staff->idx() : muse::nidx;
        interaction->setHitElementContext({item, staff});
        const auto selection = interaction->selection();
        // setHitElementContext records the menu anchor, not highlight or layout.
        // Only the two existing preserve-selection branches can reuse a raster.
        if (selection->isRange() && (selection->range()->containsPoint(logical) ||
            (item && selection->range()->containsItem(item, staffIndex)))) {
            canRetainRaster = !interaction->isGripEditStarted();
            return QStringLiteral("range");
        }
        if (item && item->selected()) {
            canRetainRaster = !interaction->isGripEditStarted();
            return QStringLiteral("selection");
        }
        if (item && (!item->isMeasure() || staffIndex != muse::nidx)) {
            interaction->select({item}, SelectType::SINGLE, staffIndex);
            return item->isMeasure() ? QStringLiteral("measure") : QStringLiteral("selection");
        }
        interaction->clearSelection();
        return {};
    }

    void dragGrip(const QPointF& from, const QPointF& to)
    {
        check(editable() && !isInput(), "grip drag requires selection mode");
        const auto interaction = m_notation->interaction();
        if (!hasSupportedGrip()) {
            return;
        }
        const QPointF a = view().inverted().map(from);
        const QPointF b = view().inverted().map(to);
        const muse::PointF first(a.x(), a.y()), last(b.x(), b.y());
        if (!interaction->isHitGrip(first)) {
            return;
        }
        interaction->startEditGrip(first);
        auto* item = interaction->selection()->element();
        interaction->startDrag({item}, item->offset(), [item](const auto* el){return el == item;});
        interaction->drag(first, last, mu::notation::DragMode::BothXY);
        interaction->endDrag();
    }

    void lasso(const QPointF& from, const QPointF& to)
    {
        check(editable() && !isInput(), "range lasso requires selection mode");
        const auto inv = view().inverted();
        const auto rect = QRectF(inv.map(from), inv.map(to)).normalized();
        if (rect.width() <= 0 || rect.height() <= 0) {
            return;
        }
        score()->lassoSelect(muse::RectF::fromQRectF(rect));
        score()->lassoSelectEnd();
    }

    using PointerPosition = muse::PointF;

    bool lassoOnVisiblePage(const PointerPosition& from, const PointerPosition& to)
    {
        using namespace mu::engraving;
        auto* page = score()->pages().at(size_t(m_page));
        const muse::RectF bounds(from - page->pos(), to - page->pos());
        const auto rect = bounds.normalized();
        if (rect.width() <= 0 || rect.height() <= 0) return false;
        // Same containment and ADD selection as Score::lassoSelect. The
        // pointer protocol already identifies the single displayed page.
        std::vector<EngravingItem*> members;
        for (auto* item : page->items(rect)) {
            if (!item->isMeasure() && item->selectable() && rect.contains(item->pageBoundingRect())) {
                members.push_back(item);
            }
        }
        score()->select(nullptr, SelectType::SINGLE, 0);
        score()->select(members, SelectType::ADD, 0);
        score()->update();
        return true;
    }

    QRectF m_pointerDamage;

    struct PointerContext {
        PointerContext(Qt::KeyboardModifiers modifiers, bool cancel = false)
        {
            mu::notation::setAstraPointerContext(modifiers, cancel);
        }
        ~PointerContext() { mu::notation::clearAstraPointerContext(); }
    };

    bool pointerActive() const { return m_pointer.id > 0; }
    bool abortPointer(const QJsonObject& packet)
    {
        if (!pointerActive() || m_pointer.id != packet.value("gesture").toInt()) return false;
        // Exceptional recovery only. A stale packet for another gesture
        // must never finalize or roll back the active one.
        auto cancel = packet;
        cancel.insert("phase", "cancel");
        cancel.insert("page", m_page + 1);
        cancel.insert("pointerV2", m_pointer.fast);
        cancel.insert("x", 0); cancel.insert("y", 0);
        cancel.insert("shift", false); cancel.insert("control", false); cancel.insert("alt", false);
        handlePointer(cancel);
        return true;
    }

    QJsonObject handlePointer(const QJsonObject& body)
    {
        using namespace mu::notation;
        using namespace mu::engraving;
        const auto interaction = m_notation->interaction();
        const auto selection = interaction->selection();
        const auto phase = body.value("phase").toString();
        const bool fast = body.value("pointerV2").toBool(false);
        ViewportScope viewport(m_page);
        const auto id = body.value("gesture").toInt();
        const auto page = body.value("page").toInt();
        check(editable() && !isInput() && !interaction->isTextEditingStarted() && id > 0,
              "score pointer requires edit mode outside note or text input");
        check(page == pageIndex() + 1, "score pointer page changed");
        const auto mayTurnPage = [&]() {
            return m_pointer.kind == "range" || m_pointer.kind == "range-copy" || m_pointer.kind == "grip" ||
                   (m_pointer.kind == "element" && (m_pointer.ottava || m_pointer.ottavaUndo));
        };
        if (phase == "page") {
            // Change only this gesture's raster viewport; its input controller
            // and native edit command keep the original global score coordinates.
            check(pointerActive() && m_pointer.id == id && mayTurnPage() && m_pointer.fast == fast, "there is no paged score drag");
            const int delta = body.value("delta").toInt();
            check((delta == 1 || delta == -1) && pageIndex() + delta >= 0 &&
                  pageIndex() + delta < pageCount(), "invalid pointer page turn");
            setPage(pageIndex() + delta);
            return {{"pointerKind", m_pointer.kind}, {"pointerAccepted", true}, {"pointerPage", pageIndex() + 1}};
        }
        check(phase == "begin" || phase == "move" || phase == "end" || phase == "cancel", "invalid pointer phase");
        const auto x = body.value("x").toDouble(-1), y = body.value("y").toDouble(-1);
        check(x >= 0 && x < 960 && y >= 0 && y < 1800, "invalid pointer point");
        Qt::KeyboardModifiers modifiers;
        for (const auto* key : {"shift", "control", "alt"}) {
            check(body.value(key).isBool(), "invalid pointer modifier");
        }
        if (body.value("shift").toBool()) modifiers |= Qt::ShiftModifier;
        if (body.value("control").toBool()) modifiers |= Qt::ControlModifier;
        if (body.value("alt").toBool()) modifiers |= Qt::AltModifier;
        const auto raw = view().inverted().map(QPointF(x, y));
        const PointerPosition logical(raw.x(), raw.y());
        PointerContext nativeModifiers(modifiers, phase == "cancel");

        if (phase == "begin") {
            check(!pointerActive(), "another score drag already started");
            const auto first = body.value("firstMove").toObject();
            if (fast && !first.isEmpty()) {
                check(first.value("x").toDouble(-1) >= 0 && first.value("x").toDouble(-1) < 960 &&
                      first.value("y").toDouble(-1) >= 0 && first.value("y").toDouble(-1) < 1800 &&
                      first.value("shift").isBool() && first.value("control").isBool() && first.value("alt").isBool(),
                      "invalid initial drag delta");
            }
            const bool shift = bool(modifiers & Qt::ShiftModifier);
            const bool control = bool(modifiers & Qt::ControlModifier);
            const auto cssScale = body.value("cssScale").toDouble(0.0);
            auto* hit = interaction->hitElement(logical, pickWidth(cssScale));
            auto* staff = interaction->hitStaff(logical);
            interaction->setHitElementContext({hit, staff});
            if (hasSupportedGrip() && interaction->isHitGrip(logical)) {
                interaction->startEditGrip(logical);
                auto* item = selection->element();
                check(item != nullptr, "score drag has no grip owner");
                m_pointer = {id, "grip", logical, logical, modifiers, {}, true};
                captureOttavaGeometry(item);
                interaction->startDrag({item}, item->offset(), [item](const auto* el) { return el == item; });
            } else if (selection->isRange() &&
                       (((!hit || hit->isMeasure()) && selection->range()->containsPoint(logical))
                        || modifiers.testFlag(Qt::AltModifier))) {
                // Stock press precedence: press on selected empty staff area starts
                // an outgoing range (drag copy only if Alt is down on drop).
                // Alt-origin outside that area is PasteRangeOnRelease instead.
                const bool fromRange = (!hit || hit->isMeasure()) && selection->range()->containsPoint(logical);
                const QString kind = fromRange ? "range" : "range-copy";
                QByteArray data;
                if (fast) {
                    const auto range = selection->range();
                    if (!interaction->startDropRange(range->startTick(), range->endTick()-range->startTick(),
                          range->startStaffIndex(), range->endStaffIndex()-range->startStaffIndex(), false)) {
                        return {{"pointerKind", "blocked"}, {"pointerAccepted", false}};
                    }
                    // Delay copying the actual music until drop. Scanning or
                    // serializing a long selection is not pointer preview work.
                } else {
                    const std::unique_ptr<QMimeData> mime(selection->qMimeData());
                    data = mime ? mime->data(mu::commonscene::MIME_STAFFLLIST_FORMAT) : QByteArray();
                    if (!selection->canCopy() || data.isEmpty() || !interaction->startDropRange(data)) {
                        return {{"pointerKind", "blocked"}, {"pointerAccepted", false}};
                    }
                }
                m_pointer = {id, kind, logical, logical, modifiers, data, false};
            } else {
                if (hit && hit->isMovable()) {
                    // Retain an already-selected item/passage just as press
                    // does for selected material; unselected objects must
                    // first pass the established native single/range logic.
                    // Unlike a completed simple click, a body press must not arm a grip.
                    // Clear EditData as well so native drag() doesn't editDrag it twice.
                    if (interaction->isGripEditStarted()) interaction->endEditElement();
                    if (!hit->selected() || shift || control) click(QPointF(x, y), shift, control, false, cssScale);
                    const auto members = selection->elements();
                    if (members.empty()) {
                        return {{"pointerKind", "blocked"}, {"pointerAccepted", false}};
                    }
                    const auto type = hit->type();
                    const bool range = selection->isRange();
                    m_pointer = {id, "element", logical, logical, modifiers, {}, true};
                    if (members.size() == 1 && members.front() == hit) captureOttavaGeometry(hit);
                    interaction->startDrag(members, hit->offset(), [range, type](const auto* element) {
                        return element && element->selected() && (!range || element->type() == type);
                    });
                } else if (!hit && (fast && !first.isEmpty() ? first.value("shift").toBool() : shift)) {
                    m_pointer = {id, "lasso", logical, logical, modifiers, {}, true};
                    if (fast) {
                        m_pointer.priorSelection = std::make_unique<mu::engraving::Selection>(score()->selection());
                    } else {
                        interaction->startDrag({}, PointerPosition(), [](const auto*) { return false; });
                    }
                } else {
                    return {{"pointerKind", "pan"}, {"pointerAccepted", true}};
                }
            }
            m_pointer.fast = fast;
            if (fast && !first.isEmpty()) {
                auto packet = body;
                packet.remove("firstMove");
                for (auto i = first.constBegin(); i != first.constEnd(); ++i) {
                    if (i.key() == "x" || i.key() == "y" || i.key() == "shift" ||
                        i.key() == "control" || i.key() == "alt") packet.insert(i.key(), i.value());
                }
                packet.insert("phase", "move");
                auto moved = handlePointer(packet);
                moved.insert("pointerMayTurnPage", mayTurnPage());
                return moved;
            }
            return {{"pointerKind", m_pointer.kind}, {"pointerAccepted", true}, {"pointerMayTurnPage", mayTurnPage()}};
        }

        check(pointerActive() && m_pointer.id == id, "score drag session changed");
        check(m_pointer.fast == fast, "score drag capability changed");
        const QString kind = m_pointer.kind;
        bool accepted = phase != "cancel";
        const bool same = logical == m_pointer.last && modifiers == m_pointer.modifiers;
        if (fast && kind == "lasso") {
            if (phase != "cancel" && (!same || phase == "end")) {
                accepted = lassoOnVisiblePage(m_pointer.first, logical);
                m_pointer.last = logical;
                m_pointer.modifiers = modifiers;
            }
            if (phase == "end" && accepted) {
                score()->lassoSelectEnd();
                score()->update();
            } else if (phase == "cancel" || (phase == "end" && !accepted)) {
                // Cancel is not a release: discard temporary marks, including
                // when a list/range was already selected before mouse down.
                score()->setSelection(*m_pointer.priorSelection);
                score()->setUpdateAll();
                score()->update();
            }
            if (phase == "end" || phase == "cancel") m_pointer = {};
            return {{"pointerKind", kind}, {"pointerAccepted", accepted},
                    {"pointerNoPaint", same && phase == "move"}};
        }
        if (fast && phase == "move" && same) {
            return {{"pointerKind", kind}, {"pointerAccepted", m_pointer.lastAccepted}, {"pointerNoPaint", true}};
        }
        if (fast) m_pointer.lastAccepted = true;
        bool watchMotion = false, motionChanged = true;
        if (kind == "range" || kind == "range-copy") {
            const bool move = kind == "range" && !modifiers.testFlag(Qt::AltModifier);
            if (fast) {
                // Clip preview time anchors and outline traversal to the
                // displayed page. The release still validates the full span.
                RangeMotionScope preview(true);
                if (phase == "move") {
                    accepted = interaction->updateDropRange(logical, false);
                    m_pointer.last = logical;
                    m_pointer.modifiers = modifiers;
                    m_pointer.lastAccepted = accepted;
                } else if (phase == "end" && selection->isRange() && selection->canCopy()) {
                    const std::unique_ptr<QMimeData> mime(selection->qMimeData());
                    const auto data = mime ? mime->data(mu::commonscene::MIME_STAFFLLIST_FORMAT) : QByteArray();
                    const auto before = undoIndex();
                    accepted = !data.isEmpty() && interaction->dropRange(data, logical, move) && undoIndex() != before;
                } else {
                    accepted = false;
                }
                if (phase == "end" || phase == "cancel") {
                    interaction->endDrop();
                    m_pointer = {};
                }
                return {{"pointerKind", kind}, {"pointerAccepted", accepted}, {"pointerAction", move ? "move" : "copy"}};
            }
            if (phase != "cancel") accepted = interaction->updateDropRange(logical);
            if (phase == "end" && accepted) {
                const auto before = undoIndex();
                // Exactly the original native internal range command. The
                // native macro owns source clearing and destination insertion.
                accepted = interaction->dropRange(m_pointer.mime, logical, move) && undoIndex() != before;
            }
            if (phase == "end" || phase == "cancel") interaction->endDrop();
            if (phase == "end" || phase == "cancel") m_pointer = {};
            return {{"pointerKind", kind}, {"pointerAccepted", accepted}, {"pointerAction", move ? "move" : "copy"}};
        } else {
            // MuseScore's Note::verticalDrag only changes the note (and lays it
            // out) when the pointer crosses a staff step; between steps nothing
            // changes. Compare the dragged items so such moves skip the repaint.
            watchMotion = fast && phase == "move" && (kind == "element" || kind == "grip");
            if (phase != "cancel" && (logical != m_pointer.last || modifiers != m_pointer.modifiers)) {
                const QByteArray beforeMotion = watchMotion ? dragSignature() : QByteArray();
                const auto beforeSystems = watchMotion ? pageSystems() : std::vector<std::pair<const void*, QRectF>>();
                const QRectF beforeItems = watchMotion ? selectionBox() : QRectF();
                const auto mode = modifiers & Qt::ShiftModifier ? DragMode::OnlyY
                                : modifiers & Qt::ControlModifier ? DragMode::OnlyX : DragMode::BothXY;
                if (m_pointer.ottava != nullptr) {
                    // First undo child runs last on undo/rollback; native time
                    // anchors and grip properties are replayed before restoring
                    // original geometry and styled flags for all old segments.
                    if (auto* undo = score()->undoStack(); undo && undo->activeCommand()) {
                        m_pointer.ottavaUndo = m_pointer.ottava.get();
                        undo->pushWithoutPerforming(m_pointer.ottava.release());
                    }
                }
                interaction->drag(m_pointer.first, logical, kind == "lasso" ? DragMode::BothXY : mode);
                if (m_pointer.ottavaUndo) m_pointer.ottavaUndo->includeCurrentCoverage();
                m_pointer.last = logical;
                m_pointer.modifiers = modifiers;
                if (watchMotion) motionChanged = dragSignature() != beforeMotion;
                m_pointerDamage = QRectF();
                if (watchMotion && motionChanged && pageSystems() == beforeSystems)
                    m_pointerDamage = dragDamage(beforeSystems, beforeItems);
            }
            if ((phase == "end" || phase == "cancel") && m_pointer.started) {
                if (phase == "end" && m_pointer.ottavaUndo) {
                    if (auto* undo = score()->undoStack(); undo && undo->activeCommand()) {
                        undo->pushWithoutPerforming(m_pointer.ottavaUndo->after());
                    }
                }
                interaction->endDrag();
            }
        }
        if (phase == "end" || phase == "cancel") m_pointer = {};
        return {{"pointerKind", kind}, {"pointerAccepted", accepted},
                {"pointerNoPaint", watchMotion && !motionChanged}};
    }

    // Every system on the shown page with its canvas box. If any system moved
    // or changed size during a drag step, the step is repainted in full.
    std::vector<std::pair<const void*, QRectF>> pageSystems() const
    {
        std::vector<std::pair<const void*, QRectF>> out;
        const auto& pages = m_notation->elements()->pages();
        if (m_page < 0 || size_t(m_page) >= pages.size()) return out;
        for (const auto* system : pages.at(size_t(m_page))->systems())
            out.emplace_back(system, system->canvasBoundingRect().toQRectF());
        return out;
    }

    QRectF selectionBox() const
    {
        QRectF box;
        for (const auto* e : m_notation->interaction()->selection()->elements())
            if (e) box |= e->canvasBoundingRect().toQRectF();
        return box;
    }

    // MuseScore lays out only the dragged chord's range (Note::verticalDrag ->
    // triggerLayout). With every system box unchanged, that can only alter the
    // systems holding the dragged items, their tied notes, and the slurs/ties
    // and other spanners attached to them. Repaint each such system together
    // with its neighbouring systems, at full page width, plus the dragged
    // items' old and new boxes. Otherwise empty (full repaint).
    QRectF dragDamage(const std::vector<std::pair<const void*, QRectF>>& systems, const QRectF& beforeItems) const
    {
        using namespace mu::engraving;
        std::set<const void*> touched;
        bool unknown = false;
        const auto addItem = [&](const EngravingItem* e) {
            const EngravingItem* sys = e ? e->findAncestor(ElementType::SYSTEM) : nullptr;
            if (sys) touched.insert(sys); else unknown = true;
        };
        const auto addSpanner = [&](const Spanner* sp) {
            for (const auto* seg : sp->spannerSegments()) addItem(seg);
        };
        for (const auto* e : m_notation->interaction()->selection()->elements()) {
            if (!e) continue;
            addItem(e);
            if (e->isSpannerSegment()) addSpanner(toSpannerSegment(e)->spanner());
            if (!e->isNote()) continue;
            for (const auto* tied : toNote(e)->tiedNotes()) addItem(tied);
            const auto* chord = toNote(e)->chord();
            const int t = chord->tick().ticks();
            for (const auto& iv : score()->spannerMap().findOverlapping(t, t + 1)) {
                const Spanner* sp = iv.value;
                const auto* a = sp->startElement();
                const auto* b = sp->endElement();
                const auto involves = [&](const EngravingItem* x) {
                    return x && (x == e || x == chord || (x->isNote() && toNote(x)->chord() == chord));
                };
                if (involves(a) || involves(b)) addSpanner(sp);
            }
            for (const auto* sp : toNote(e)->spannerFor()) addSpanner(sp);
            for (const auto* sp : toNote(e)->spannerBack()) addSpanner(sp);
        }
        if (unknown || touched.empty()) return QRectF();
        const auto& pages = m_notation->elements()->pages();
        const auto pageBox = pages.at(size_t(m_page))->canvasBoundingRect().toQRectF();
        QRectF damage = beforeItems | selectionBox();
        for (size_t i = 0; i < systems.size(); ++i) {
            if (!touched.count(systems[i].first)) continue;
            // Items of a system can reach into its neighbours' boxes (high
            // ledger lines, slurs): cover both neighbouring systems entirely.
            const double top = i ? systems[i-1].second.top() : pageBox.top();
            const double bottom = i + 1 < systems.size() ? systems[i+1].second.bottom() : pageBox.bottom();
            damage |= QRectF(pageBox.left(), top, pageBox.width(), bottom - top);
            touched.erase(systems[i].first);
        }
        // A touched system on another page (a tie or slur over a page turn).
        if (!touched.empty()) return QRectF();
        return damage;
    }

    // Everything a drag step can change about the dragged items: pitch and
    // spelling, fret/string, user offset, and their laid-out position and box.
    QByteArray dragSignature() const
    {
        using namespace mu::engraving;
        QByteArray out;
        const auto put = [&out](double v) { out.append(reinterpret_cast<const char*>(&v), sizeof v); };
        for (const auto* e : m_notation->interaction()->selection()->elements()) {
            if (!e) continue;
            const auto p = e->pagePos();
            const auto b = e->ldata()->bbox();
            put(double(int(e->type()))); put(p.x()); put(p.y());
            put(b.x()); put(b.y()); put(b.width()); put(b.height());
            put(e->offset().x()); put(e->offset().y());
            if (e->isNote()) {
                const auto* n = toNote(e);
                put(n->pitch()); put(n->tpc1()); put(n->tpc2()); put(n->fret()); put(n->string());
                put(double(int(n->accidentalType())));
            }
        }
        return out;
    }

    size_t filterTopNoteIndex() const
    {
        using namespace mu::engraving;
        const auto selection = m_notation->interaction()->selection();
        if (!selection->isRange()) return muse::nidx;
        const auto* range = selection->range().get();
        if (!range || !range->containsMultiNoteChords()) return muse::nidx;
        const auto* end = range->rangeEndSegment();
        size_t maxIdx = 0;
        for (const auto* seg = range->rangeStartSegment(); seg && seg != end; seg = seg->next1MM()) {
            if (!seg->enabled()) continue;
            for (track_idx_t track = staff2track(range->startStaffIndex());
                 track < staff2track(range->endStaffIndex()); ++track) {
                if (const auto* item = seg->element(track); item && item->isChord()) {
                    maxIdx = std::max(maxIdx, toChord(item)->notes().size() - 1);
                }
            }
        }
        return maxIdx;
    }

    QJsonObject selectionFilterState() const
    {
        using namespace mu::engraving;
        const auto filter = m_notation->interaction()->selectionFilter();
        if (!filter) return {};
        QJsonArray elements, voices, indices;
        for (size_t i = 0; i < NUM_ELEMENTS_SELECTION_FILTER_TYPES; ++i) {
            elements.append(filter->isSelectionTypeFiltered(ElementsSelectionFilterTypes(1u << i)));
        }
        for (size_t i = 0; i < NUM_VOICES_SELECTION_FILTER_TYPES; ++i) {
            voices.append(filter->isSelectionTypeFiltered(VoicesSelectionFilterTypes(1u << i)));
        }
        const size_t maxIdx = filterTopNoteIndex();
        for (size_t i = 0; i < NUM_NOTES_IN_CHORD_SELECTION_FILTER_TYPES; ++i) {
            if (maxIdx == muse::nidx || (i > 0 && NUM_NOTES_IN_CHORD_SELECTION_FILTER_TYPES - 1 - i >= maxIdx)) {
                indices.append(QJsonValue(QJsonValue::Null));
                continue;
            }
            bool filtered = filter->isSelectionTypeFiltered(NotesInChordSelectionFilterTypes(1u << i));
            if (i == 0 && maxIdx < NUM_NOTES_IN_CHORD_SELECTION_FILTER_TYPES - 1) {
                filtered = filtered || filter->isSelectionTypeFiltered(
                    NotesInChordSelectionFilterTypes(1u << (NUM_NOTES_IN_CHORD_SELECTION_FILTER_TYPES - 1 - maxIdx)));
            }
            indices.append(filtered);
        }
        return {{"elements",elements},{"voices",voices},{"indices",indices},
                {"single",filter->includeSingleNotes()},{"hasChords",maxIdx != muse::nidx}};
    }

    void editSelectionFilter(const QJsonObject& body)
    {
        using namespace mu::engraving;
        check(editable() && !isInput(), "selection filters require selection mode");
        const auto f = m_notation->interaction()->selectionFilter();
        const auto group = body.value("group").toString();
        const int i = body.value("index").toInt(-2);
        const int max = group == "elements" ? int(NUM_ELEMENTS_SELECTION_FILTER_TYPES)
                        : group == "voices" ? int(NUM_VOICES_SELECTION_FILTER_TYPES)
                        : group == "indices" ? int(NUM_NOTES_IN_CHORD_SELECTION_FILTER_TYPES) : 0;
        check(bool(f) && body.value("include").isBool(), "invalid selection filter");
        const bool include = body.value("include").toBool();
        if (group == "single") {
            check(filterTopNoteIndex() != muse::nidx, "select notes in a chord first");
            f->setIncludeSingleNotes(include);
        } else {
            check(max > 0 && i >= -1 && i < max, "invalid selection filter index");
            const auto bits = i == -1 ? ((1u << max) - 1) : 1u << i;
            if (group == "elements") {
                f->setSelectionTypeFiltered(ElementsSelectionFilterTypes(bits),include);
            } else if (group == "voices") {
                f->setSelectionTypeFiltered(VoicesSelectionFilterTypes(bits),include);
            } else {
                const size_t top = filterTopNoteIndex();
                check(top != muse::nidx && (i <= 0 || size_t(max - 1 - i) < top), "no such chord note in the selected range");
                f->setSelectionTypeFiltered(NotesInChordSelectionFilterTypes(bits),include);
                if (i == -1) {
                    f->setIncludeSingleNotes(include);
                } else if (i == 0 && top < size_t(max-1)) {
                    f->setSelectionTypeFiltered(NotesInChordSelectionFilterTypes(1u << (max-1-top)),include);
                }
            }
        }
    }

    static const mu::engraving::System* nativeSelectionSystem(const mu::engraving::EngravingItem* item)
    {
        using namespace mu::engraving;
        if (item && item->isSpannerSegment()) {
            if (auto* system = toSpannerSegment(item)->system()) return system;
        }
        for (const auto* parent = item; parent; parent = parent->parentItem()) {
            if (parent->isSystem()) return static_cast<const System*>(parent);
        }
        return nullptr;
    }

    // Stock SelectNoteDialog and SelectDialog supply pattern options over the
    // original context element, then invoke the same native filter engine.
    mu::engraving::EngravingItem* selectionMoreContext() const
    {
        const auto interaction = m_notation->interaction();
        if (auto* item = interaction->contextItem()) return item;
        // From the keyboard in a list selection there may be no hit context;
        // use an actual selected member rather than presenting an inert menu.
        const auto& elements = interaction->selection()->elements();
        return elements.empty() ? nullptr : elements.front();
    }

    QJsonObject selectionMoreState() const
    {
        using namespace mu::engraving;
        const auto interaction = m_notation->interaction();
        const auto current = interaction->selection();
        const auto* item = selectionMoreContext();
        if (!editable() || isInput() || interaction->isTextEditingStarted() || current->isNone() || !item) return {};
        QJsonArray options;
        const auto field = [&](const char* id, const QString& name = {}) {
            options.append(QJsonArray{QString::fromLatin1(id), name});
        };
        if (item->isNote()) {
            const auto* note = toNote(item);
            field("head", TConv::translatedUserName(note->headGroup()));
            field("pitch", note->tpcUserName().toQString());
            field("name", tpc2name(note->tpc(), NoteSpellingType::STANDARD, NoteCaseType::AUTO, false));
            field("type", note->noteTypeUserName().toQString());
            field("duration-type", TConv::translatedUserName(note->chord()->durationType().type()));
            field("duration-ticks", note->chord()->durationUserName().toQString());
            // Per-string matching acts on TAB/fretted notes, not on these two piano staves.
        } else {
            if (item->subtype() != -1) field("subtype", item->translatedSubtypeUserName().toQString());
            if (item->isRest()) field("duration-ticks", toRest(item)->durationUserName().toQString());
        }
        if (item->staffIdx() != muse::nidx) field("staff");
        field("voice");
        if (nativeSelectionSystem(item)) field("system");
        field("beat");
        field("measure");
        return {{"type", item->translatedTypeUserName().toQString()}, {"options", options}, {"inSelection", !item->score()->selection().isSingle()}};
    }

    void matchSelectionMore(const QJsonObject& body)
    {
        using namespace mu::engraving;
        using namespace mu::notation;
        const QJsonObject available = selectionMoreState();
        check(!available.isEmpty(), "choose an original notation element before selecting by pattern");
        const auto interaction = m_notation->interaction();
        const auto current = interaction->selection();
        const auto* item = selectionMoreContext();
        const auto action = body.value("action").toString();
        const bool onlySelected = body.value("inSelection").toBool();
        check((action == "replace" || action == "add" || action == "subtract") &&
            body.value("inSelection").isBool() && (!onlySelected || available.value("inSelection").toBool()) &&
            body.value("criteria").isArray(), "invalid pattern selection options");
        const auto options = available.value("options").toArray();
        const auto criteria = body.value("criteria").toArray();
        check(criteria.size() <= options.size(), "too many pattern selection options");
        for (const auto& key : criteria) {
            check(key.isString() && std::any_of(options.begin(), options.end(), [&](const auto& entry) {
                return entry.toArray().at(0) == key;
            }), "invalid pattern selection criterion");
        }
        const auto set = [&](const char* name) { return criteria.contains(QString::fromLatin1(name)); };
        FilterNotesOptions notePattern;
        FilterElementsOptions elementPattern;
        auto& pattern = item->isNote() ? static_cast<FilterElementsOptions&>(notePattern) : elementPattern;
        pattern.elementType = item->type();
        if (set("staff")) {
            pattern.staffStart = int(item->staffIdx());
            pattern.staffEnd = pattern.staffStart + 1;
        } else if (onlySelected && current->isRange()) {
            // Native Selection staff bounds are only valid for RANGE.
            // LIST is intersected with its actual selected objects below.
            pattern.staffStart = int(score()->selection().staffStart());
            pattern.staffEnd = int(score()->selection().staffEnd());
        }
        if (set("voice")) pattern.voice = int(item->voice());
        if (set("system")) pattern.system = nativeSelectionSystem(item);
        if (set("beat")) pattern.beat = item->beat();
        if (set("measure")) {
            pattern.measure = item->findMeasure();
            if (!pattern.measure && item->isSpannerSegment()) {
                if (auto* spanner = toSpannerSegment(item)->spanner())
                    if (auto* start = spanner->startElement()) pattern.measure = start->findMeasure();
            }
        }
        if (item->isNote()) {
            const auto* note = toNote(item);
            if (set("head")) notePattern.notehead = note->headGroup();
            if (set("pitch")) notePattern.pitch = note->pitch();
            if (set("name")) notePattern.tpc = note->tpc();
            if (set("type")) notePattern.noteType = note->noteType();
            if (set("duration-type")) notePattern.durationType = note->chord()->actualDurationType();
            if (set("duration-ticks")) notePattern.durationTicks = note->chord()->actualTicks();
        } else {
            pattern.subtype = item->subtype();
            pattern.bySubtype = set("subtype");
            if (set("duration-ticks") && item->isRest()) pattern.durationTicks = toRest(item)->actualTicks();
        }
        auto elements = m_notation->elements()->elements(pattern);
        if (elements.empty()) return; // stock leaves the old selection untouched in this case.
        const auto previous = current->elements();
        const auto has = [](const auto& list, const EngravingItem* target) {
            return std::find(list.begin(), list.end(), target) != list.end();
        };
        if (onlySelected) {
            elements.erase(std::remove_if(elements.begin(), elements.end(), [&](auto* e) {
                return !has(previous, e);
            }), elements.end());
        }
        if (action == "add") {
            elements.insert(elements.end(), previous.begin(), previous.end());
        } else if (action == "subtract") {
            std::vector<EngravingItem*> remainder;
            for (auto* e : previous) if (!has(elements,e)) remainder.push_back(e);
            elements = std::move(remainder);
        }
        if (action != "add") interaction->clearSelection();
        interaction->select(elements,SelectType::ADD);
    }

    // Keyboard navigation derives from NotationActionController::move,
    // the related chord actions, and the direct first/last/select-all actions.
    // Astra transport owns playback separately; no native seek is implied.
    void navigate(const QString& mode, const QString& direction)
    {
        using namespace mu::notation;
        check(editable(), "navigation requires edit mode");
        const auto interaction = m_notation->interaction();
        using Direction = MoveDirection;
        const auto dir = direction == "right" ? Direction::Right : direction == "left" ? Direction::Left
                       : direction == "up" ? Direction::Up : direction == "down" ? Direction::Down
                       : Direction::Undefined;
        if (mode == "all") {
            interaction->selectAll();
        } else if (mode == "section") {
            interaction->selectSection();
        } else if (mode == "top-staff") {
            interaction->selectTopStaff();
        } else if (mode == "empty-trailing") {
            interaction->selectEmptyTrailingMeasure();
        } else if (mode == "begin-system" || mode == "end-system" ||
                   mode == "begin-score" || mode == "end-score") {
            check(!isInput(), "selection expansion requires selection mode");
            interaction->expandSelection(mode == "begin-system" ? ExpandSelectionMode::BeginSystem
                : mode == "end-system" ? ExpandSelectionMode::EndSystem
                : mode == "begin-score" ? ExpandSelectionMode::BeginScore : ExpandSelectionMode::EndScore);
        } else if (mode == "segment-element" || mode == "track" || mode == "frame" || mode == "system" || mode == "swap-chord") {
            check(dir == Direction::Right || dir == Direction::Left, "invalid notation navigation direction");
            if (mode == "segment-element") {
                interaction->moveSegmentSelection(dir);
            } else if (mode == "swap-chord") {
                check(isInput(), "moving chord to the next duration requires note entry");
                interaction->swapChordRest(dir);
            } else {
                interaction->moveSelection(dir, mode == "track" ? MoveSelectionType::Track
                                                 : mode == "frame" ? MoveSelectionType::Frame : MoveSelectionType::System);
            }
        } else if (mode == "similar" || mode == "similar-staff" || mode == "similar-range") {
            const auto chosen = interaction->selection();
            if (mode == "similar-range") {
                auto* hit = chosen->lastElementHit();
                if (!chosen->isRange() || !hit) return;
                score()->selectSimilarInRange(hit);
                if (score()->selectionChanged()) interaction->selectionChanged().notify();
                return;
            }
            const auto* selected = chosen->element();
            if (!selected) return;
            // Same filter options as NotationActionController:1917–1935.
            FilterElementsOptions options;
            options.elementType = selected->type();
            if (selected->isNote()) {
                options.subtype = mu::engraving::toNote(selected)->chord()->isGrace() ? -1 : selected->subtype();
            } else if (selected->isHairpinSegment()) {
                options.subtype = selected->subtype();
                options.bySubtype = true;
            }
            if (mode == "similar-staff") {
                options.staffStart = int(selected->staffIdx());
                options.staffEnd = options.staffStart + 1;
            }
            const auto elements = m_notation->elements()->elements(options);
            if (elements.empty()) return;
            interaction->clearSelection();
            interaction->select(elements, SelectType::ADD);
            return;
        } else if (mode == "first") {
            interaction->selectFirstElement(false);
        } else if (mode == "last") {
            interaction->selectLastElement();
        } else if (mode == "chord-edge" || mode == "chord-note") {
            check(dir == Direction::Up || dir == Direction::Down, "invalid chord navigation");
            if (!interaction->selection()->isNone()) {
                mode == "chord-edge" ? interaction->selectTopOrBottomOfChord(dir) : interaction->moveChordNoteSelection(dir);
            }
        } else {
            check((dir == Direction::Right || dir == Direction::Left)
                  && (mode == "chord" || mode == "measure" || mode == "element"), "invalid notation navigation");
            const auto* item = interaction->selection()->element();
            if (mode == "element" || (interaction->selection()->isNone() && score()->selection().currentCR())) {
                interaction->moveSelection(dir, MoveSelectionType::EngravingItem);
            } else if (item && item->isTextBase()) {
                interaction->nudge(dir, mode == "measure");
            } else if (item && item->hasGrips() && interaction->isGripEditStarted()) {
                interaction->nudgeAnchors(dir);
            } else {
                if (interaction->selection()->isNone()) interaction->selectFirstElement(false);
                interaction->moveSelection(dir, mode == "measure" ? MoveSelectionType::Measure : MoveSelectionType::Chord);
            }
        }
        const auto* item = interaction->selection()->element();
        const auto* segment = score()->selection().activeSegment();
        const auto* measure = item ? item->findMeasure() : segment ? segment->measure() : nullptr;
        if (mode != "all" && measure && measure->system()) {
            const auto& pages = m_notation->elements()->pages();
            const auto p = std::find(pages.begin(), pages.end(), measure->system()->page());
            if (p != pages.end()) setPage(int(std::distance(pages.begin(), p)));
        }
    }

    void editLineKey(const QJsonObject& body)
    {
        check(editable() && !isInput() && hasSupportedGrip(), "请选择带可编辑控制点的记号");
        const auto key = body.value("key").toString();
        const QMap<QString, int> keys {{"ArrowLeft",Qt::Key_Left},{"ArrowRight",Qt::Key_Right},
                                      {"ArrowUp",Qt::Key_Up},{"ArrowDown",Qt::Key_Down},
                                      {"Tab",Qt::Key_Tab}};
        check(keys.contains(key) && body.value("shift").isBool() && body.value("control").isBool(),
              "invalid line edit key");
        Qt::KeyboardModifiers mods;
        if (body.value("shift").toBool()) mods |= Qt::ShiftModifier;
        if (body.value("control").toBool()) mods |= Qt::ControlModifier;
        QKeyEvent event(QEvent::KeyPress, key == "Tab" && mods.testFlag(Qt::ShiftModifier)
                         ? int(Qt::Key_Backtab) : keys.value(key), mods);
        auto interaction = m_notation->interaction();
        if (interaction->isEditAllowed(&event)) interaction->editElement(&event);
    }

    void expandSelection(const QString& direction, const QString& unit, bool control = false)
    {
        using namespace mu::notation;
        check(editable() && !isInput(), "keyboard range requires selection mode");
        const auto interaction = m_notation->interaction();
        if (interaction->selection()->isNone()) {
            return;
        }
        const bool horizontal = direction == "right" || direction == "left";
        check(horizontal ? unit == "chord" || unit == "measure"
                         : ((direction == "up" || direction == "down") && unit == "staff"),
              "invalid keyboard range direction");
        const auto dir = direction == "right" ? MoveDirection::Right : direction == "left" ? MoveDirection::Left
                       : direction == "up" ? MoveDirection::Up : MoveDirection::Down;
        if (horizontal) {
            const auto* item = interaction->selection()->element();
            Qt::KeyboardModifiers mods = Qt::ShiftModifier;
            if (control) mods |= Qt::ControlModifier;
            QKeyEvent key(QEvent::KeyPress, dir == MoveDirection::Right ? Qt::Key_Right : Qt::Key_Left, mods);
            if (interaction->isElementEditStarted() && interaction->isEditAllowed(&key)) {
                interaction->editElement(&key);
                return;
            }
            if (item && item->allowTimeAnchor()) {
                interaction->moveElementAnchors(&key);
                return;
            }
        }
        const auto type = unit == "chord" ? MoveSelectionType::Chord : unit == "measure" ? MoveSelectionType::Measure
                      : MoveSelectionType::Track;
        interaction->addToSelection(dir, type);
        const auto* active = score()->selection().activeSegment();
        const auto* measure = active ? active->measure() : nullptr;
        const auto* sys = measure ? measure->system() : nullptr;
        if (sys) {
            const auto& pages = m_notation->elements()->pages();
            const auto page = std::find(pages.begin(), pages.end(), sys->page());
            if (page != pages.end()) {
                setPage(int(std::distance(pages.begin(), page)));
            }
        }
    }

    QJsonObject transposeState() const
    {
        using namespace mu::engraving;
        const auto selection=m_notation->interaction()->selection();
        const bool all=selection->isNone();
        const bool range=selection->isRange();
        const auto* staff=score()->staff(range ? selection->range()->startStaffIndex() : 0);
        const auto tick=range ? selection->range()->startTick() : Fraction(0,1);
        const bool harmony=std::any_of(selection->elements().cbegin(),selection->elements().cend(),
            [](const EngravingItem* e){return e && e->isHarmony();});
        return {{"all",all},{"range",range||all},{"key",staff ? int(staff->concertKey(tick)) : 0},{"hasHarmony",harmony||all}};
    }

    void transpose(const QJsonObject& body)
    {
        using namespace mu::engraving;
        using namespace mu::notation;
        check(editable() && !isInput(), "exit note input before transposing");
        const auto interaction=m_notation->interaction();
        const auto state=transposeState();
        const auto mode=body.value("mode").toString(), direction=body.value("direction").toString();
        const auto key=body.value("key").toInt(99), interval=body.value("interval").toInt(-1);
        const bool toKey=mode=="key", diatonic=mode=="diatonic", range=state.value("range").toBool();
        check(toKey ? range && key>=-7 && key<=7 : diatonic ? interval>=1 && interval<=6
              : mode=="chromatic" && interval>=0 && interval<26, "invalid transposition target");
        check(direction=="up" || direction=="down" || (toKey && direction=="closest"), "invalid transposition direction");
        for (const auto* option : {"transposeKeys","chordNames","useDouble"}) {
            check(body.value(option).isBool(), "invalid transposition options");
        }
        const bool all=state.value("all").toBool();
        if (all) interaction->selectAll();
        const auto history=m_notation->undoStack();
        history->prepareChanges(muse::TranslatableString("undoableAction", "Transposition"));
        bool ok=false;
        try {
            // Same engraving operation as NotationInteraction::transpose.
            // Roll back a partial native failure before returning across RPC.
            ok=score()->transpose(toKey ? TransposeMode::TO_KEY : diatonic ? TransposeMode::DIATONICALLY : TransposeMode::BY_INTERVAL,
                direction=="up" ? TransposeDirection::UP : direction=="down" ? TransposeDirection::DOWN : TransposeDirection::CLOSEST,
                Key(toKey ? key : 0), interval,
                (range||diatonic)&&body.value("transposeKeys").toBool(),
                body.value("chordNames").toBool(),!diatonic&&body.value("useDouble").toBool());
        } catch (...) {
            history->rollbackChanges();
            if (all) interaction->clearSelection();
            throw;
        }
        if (!ok) {
            history->rollbackChanges();
            if (all) interaction->clearSelection();
            check(false,"transposition is outside the supported pitch range");
            return;
        }
        history->commitChanges();
        if (all) interaction->clearSelection();
        m_notation->notationChanged().notify();
    }

    QJsonObject systemLayoutState() const
    {
        if (!m_editable || isInput()) return {};
        const auto& selection = score()->selection();
        const auto* first = selection.startMeasureBase();
        const auto* last = selection.endMeasureBase();
        const auto* sys = first ? first->system() : nullptr;
        if (!first || !last || !sys || !last->system()) return {};
        return {{"previous", bool(last->prevNonVBoxSystem())},
                {"next", bool(first->nextNonVBoxSystem())},
                {"make", selection.isRange()}, {"locked", sys->isLocked()},
                {"multiple", sys != last->system()}, {"number", first->isMeasure() ? mu::engraving::toMeasure(first)->no() + 1 : -1}};
    }

    void applyTool(const QString& tool, const QString& value)
    {
        using namespace mu::notation;
        using namespace mu::engraving;
        check(m_editable, "请先开启编辑模式");
        const auto interaction = m_notation->interaction();
        const auto input = interaction->noteInput();
        const auto startInput = [&] {
            input->startNoteInput(NoteInputMethod::BY_NOTE_NAME, false);
            check(isInput(), "此处不能开始音符输入");
        };
        if (tool == "system-lock-plan") {
            check(!isInput() && score()->firstMeasure(), "exit note input before changing system layout");
            int interval = 0;
            auto mode = AddRemoveSystemLockType::None;
            if (value == "existing") {
                mode = AddRemoveSystemLockType::AfterEachSystem;
            } else if (value != "remove") {
                bool ok = false;
                const auto count = value.mid(9);
                interval = count.toInt(&ok);
                check(value.startsWith("interval:") && ok && QString::number(interval) == count
                          && interval >= 1 && interval <= 99, "invalid measures per system");
                mode = AddRemoveSystemLockType::MeasuresInterval;
            }
            // BreaksDialog::showEvent/accept: an existing range is the target;
            // otherwise operate on the whole score, then clear temporary selection.
            const bool fullScore = !interaction->selection()->isRange();
            if (fullScore) interaction->selectAll();
            interaction->addRemoveSystemLocks(mode, interval);
            if (fullScore) interaction->clearSelection();
            return;
        }
        if (tool == "system-layout") {
            const auto state = systemLayoutState();
            check(!state.isEmpty(), "select a measure before changing system layout");
            const auto before = undoIndex();
            if (value == "previous" && state.value("previous").toBool()) {
                interaction->moveMeasureToPrevSystem();
            } else if (value == "next" && state.value("next").toBool()) {
                interaction->moveMeasureToNextSystem();
            } else if (value == "make" && state.value("make").toBool()) {
                interaction->makeIntoSystem();
            } else if (value == "lock") {
                interaction->toggleSystemLock();
            } else {
                check(false, "system layout action unavailable here");
            }
            check(before != undoIndex(), "system layout unchanged");
            return;
        }
        if (tool == "input") {
            if (isInput()) {
                input->endNoteInput();
            } else {
                startInput();
            }
            return;
        }
        if (tool == "cross-staff") {
            const auto selection = interaction->selection();
            // In stock 4.6.5 the action visits ChordRest objects, not only Notes.
            // Keep that same set for mixed lists and all-rest selected ranges.
            const auto elements = selection->elements();
            const bool rhythm = std::any_of(elements.cbegin(), elements.cend(),
                                            [](const auto* e) { return e && (e->isChordRest() || e->isNote()); });
            check(!isInput() && rhythm && (value == "up" || value == "down"),
                  "select a note, rest, or passage to move to the other displayed staff");
            interaction->moveChordRestToStaff(value == "up" ? MoveDirection::Up : MoveDirection::Down);
            return;
        }
        if (tool == "swap-voices") {
            check(!isInput() && interaction->selection()->isRange() && value.size() == 2,
                  "select a passage to exchange voices");
            const int a = value[0].digitValue(), b = value[1].digitValue();
            check(a >= 1 && a <= VOICES && b >= 1 && b <= VOICES && a != b, "invalid voice pair");
            interaction->swapVoices(voice_idx_t(a - 1), voice_idx_t(b - 1));
            return;
        }
        if (tool == "stage-pitch") {
            check(isInput() && input->usingNoteInputMethod(NoteInputMethod::BY_DURATION), "stage pitch requires duration input");
            check(value == "up" || value == "down" || value == "up-octave" || value == "down-octave", "invalid staged pitch direction");
            input->setRestMode(false);
            input->moveInputNotes(value.startsWith("up"), value.endsWith("-octave") ? PitchMode::OCTAVE : PitchMode::DIATONIC);
            return;
        }
        if (tool == "note-letter" || tool == "chord-letter" || tool == "insert-letter") {
            check(value.size() == 1 && value[0].toLatin1() >= 'a' && value[0].toLatin1() <= 'g',
                  "invalid letter input");
            if (!isInput()) {
                startInput();
            }
            const char* letters = "cdefgab";
            const auto letter = std::find(letters, letters + 7, value[0].toLatin1());
            NoteInputParams params;
            const bool chordAdd = tool == "chord-letter";
            const bool insert = tool == "insert-letter";
            check(score()->resolveNoteInputParams(int(letter - letters), chordAdd, params), "no native note input location");
            // Match NotationActionController::handleNoteAction. Duration
            // input stages the letter; only the duration pad places a note.
            if (!chordAdd && !insert && input->usingNoteInputMethod(NoteInputMethod::BY_DURATION)) {
                input->setRestMode(false);
                input->setInputNote(params);
                return;
            }
            input->addNote(params, chordAdd ? NoteAddingMode::CurrentChord
                                          : insert ? NoteAddingMode::InsertChord : NoteAddingMode::NextChord);
            return;
        }
        if (tool == "input-stop") {
            if (isInput()) input->endNoteInput();
            return;
        }
        if (tool == "insert-rest") {
            // The keyboard rest command commits; Pad::REST is a distinct
            // toggle used by the mouse toolbar and by duration input.
            interaction->putRestToSelection();
            return;
        }
        if (tool == "relative-duration") {
            const bool dotted = value == "longer-dotted" || value == "shorter-dotted";
            const bool increase = value == "double" || value == "longer-dotted";
            check(increase || value == "half" || value == "shorter-dotted", "invalid duration direction");
            if (isInput() && !dotted) {
                if (increase) input->doubleNoteInputDuration();
                else input->halveNoteInputDuration();
            } else {
                interaction->increaseDecreaseDuration(increase ? -1 : 1, dotted);
            }
            return;
        }
        if (tool == "full-measure-rest") {
            interaction->execute(&Score::cmdFullMeasureRest,
                muse::TranslatableString("undoableAction", "Enter full-measure rest"));
            return;
        }
        if (tool == "select-interval") {
            bool ok = false;
            const int interval = value.toInt(&ok);
            check(ok && ((interval >= 1 && interval <= 10) || (interval >= -10 && interval <= -2)), "invalid selected-note interval");
            interaction->addIntervalToSelectedNotes(interval);
            return;
        }
        if (tool == "input-method" || tool == "toggle-timewise") {
            const bool timewiseShortcut = tool == "toggle-timewise";
            check(timewiseShortcut || value == "note-name" || value == "duration" || value == "rhythm"
                  || value == "repitch" || value == "timewise", "unsupported input method");
            const auto method = timewiseShortcut
                                ? (input->usingNoteInputMethod(NoteInputMethod::TIMEWISE)
                                   ? NoteInputMethod::BY_NOTE_NAME : NoteInputMethod::TIMEWISE)
                                : value == "duration" ? NoteInputMethod::BY_DURATION
                                : value == "rhythm" ? NoteInputMethod::RHYTHM
                                : value == "repitch" ? NoteInputMethod::REPITCH
                                : value == "timewise" ? NoteInputMethod::TIMEWISE : NoteInputMethod::BY_NOTE_NAME;
            if (!isInput()) input->startNoteInput(method, false);
            else if (input->usingNoteInputMethod(method)) input->endNoteInput();
            else input->setNoteInputMethod(method);
            return;
        }
        if (tool == "duration" || tool == "dot" || tool == "rest"
            || tool == "accidental" || tool == "voice") {
            if (!isInput() && interaction->selection()->isNone()) {
                startInput();
            }
            if (tool == "duration") {
                const auto pad = value == "long" ? Pad::NOTE00 : value == "breve" ? Pad::NOTE0
                                 : value == "128" ? Pad::NOTE128 : value == "256" ? Pad::NOTE256
                                 : value == "512" ? Pad::NOTE512 : value == "1024" ? Pad::NOTE1024
                                 : value == "whole" ? Pad::NOTE1 : value == "half" ? Pad::NOTE2
                                 : value == "quarter" ? Pad::NOTE4 : value == "eighth" ? Pad::NOTE8
                                 : value == "sixteenth" ? Pad::NOTE16 : value == "thirtysecond" ? Pad::NOTE32
                                 : value == "sixtyfourth" ? Pad::NOTE64 : Pad::REST;
                check(pad != Pad::REST, "unsupported duration");
                input->padNote(pad);
            } else if (tool == "dot") {
                const auto dots = value == "toggle" || value == "1" ? Pad::DOT : value == "2" ? Pad::DOT2
                                  : value == "3" ? Pad::DOT3 : value == "4" ? Pad::DOT4 : Pad::REST;
                check(dots != Pad::REST, "invalid augmentation dots");
                isInput() ? input->padNote(dots) : interaction->toggleDotsForSelection(dots);
            } else if (tool == "rest") {
                input->padNote(Pad::REST);
            } else if (tool == "accidental") {
                const auto accidental = value == "sharp" ? AccidentalType::SHARP : value == "flat" ? AccidentalType::FLAT
                                        : value == "natural" ? AccidentalType::NATURAL : value == "flat2" ? AccidentalType::FLAT2
                                        : value == "sharp2" ? AccidentalType::SHARP2 : AccidentalType::NONE;
                check(accidental != AccidentalType::NONE, "unsupported accidental");
                isInput() ? input->setAccidental(accidental) : interaction->toggleAccidentalForSelection(accidental);
            } else {
                bool valid = false;
                const int number = value.toInt(&valid);
                check(valid && number >= 1 && number <= VOICES, "unsupported voice");
                const auto voice = voice_idx_t(number - 1);
                input->setCurrentVoice(voice);
                if (!isInput()) {
                    interaction->changeSelectedElementsVoice(voice);
                }
            }
            return;
        }
        if (tool == "tie" || tool == "chord-tie" || tool == "lv") {
            check(isInput() || !interaction->selection()->notes().empty(), "请先选择要连接的音符");
            if (tool == "lv") {
                isInput() ? input->addLaissezVib() : interaction->addLaissezVibToSelection();
            } else if (isInput()) {
                input->addTie();
            } else if (tool == "chord-tie") {
                interaction->addTiedNoteToChord();
            } else {
                interaction->addTieToSelection();
            }
            return;
        }
        if (tool == "slur") {
            if (isInput() && input->state().slur()) {
                input->resetSlur();
                return;
            }
            check(!interaction->selection()->isNone() && !score()->getSelectedChordRests().empty(),
                  "请选择音符、休止符或乐句");
            interaction->addSlurToSelection();
            return;
        }
        if (tool == "ottava") {
            const QMap<QString, OttavaType> types {
                {"8va",OttavaType::OTTAVA_8VA}, {"8vb",OttavaType::OTTAVA_8VB},
                {"15ma",OttavaType::OTTAVA_15MA}, {"15mb",OttavaType::OTTAVA_15MB},
                {"22ma",OttavaType::OTTAVA_22MA}, {"22mb",OttavaType::OTTAVA_22MB}
            };
            check(types.contains(value) && !score()->getSelectedChordRests().empty(), "请选择音符、休止符或乐句");
            const auto before = undoIndex();
            interaction->addOttavaToSelection(types.value(value));
            check(undoIndex() != before, "此位置不能添加八度线");
            return;
        }
        if (tool == "triplet" || tool == "tuplet") {
            const QString specification = tool == "triplet" ? QStringLiteral("auto:3") : value;
            const auto tokens = specification.split(':');
            const bool shorthand = tokens.value(0) == "auto";
            const bool explicitAppearance = !shorthand && tokens.size() == 4;
            bool goodN = false, goodD = false;
            const int n = tokens.value(shorthand ? 1 : 0).toInt(&goodN);
            const int d = shorthand ? 2 : tokens.value(1).toInt(&goodD);
            check((tokens.size() == 2 || explicitAppearance) && goodN &&
                  (shorthand ? n >= 2 && n <= 9 : n >= 1 && n <= 9999 && goodD && d >= 1 && d <= 9999),
                  "连音比例无效");
            check(interaction->canAddTupletToSelectedChordRests(), "当前音符太短，不能再分连音");
            const auto isEligible = [](const ChordRest* cr) {
                return cr && !cr->isGrace() && !(cr->isChord() && toChord(cr)->isTrillCueNote());
            };
            const auto canUse = [&](const ChordRest* cr, const Fraction& group, const Fraction& actualGroup) {
                if (!cr->measure() || cr->measure()->isMMRest() || group <= Fraction(0,1) || actualGroup.ticks() < n) {
                    return false;
                }
                const int normal = shorthand ? Tuplet::computeTupletDenominator(n, group) : d;
                return normal > 0 && TDuration::isValid(group.reduced() * Fraction(1, normal));
            };
            if (isInput()) {
                const auto& noteState = score()->inputState();
                const auto* cr = noteState.cr();
                // Upstream computes ratio from the staged pad and uses changeCRlen
                // before creating the tuplet. The underlying rest may be longer.
                // Retain the validity gate so a malformed ratio cannot only resize
                // the source ChordRest and then fail to create a group.
                const auto group = noteState.ticks();
                const auto actualGroup = cr && !cr->ticks().isZero() ? group * (cr->actualTicks() / cr->ticks()) : group;
                check(isEligible(cr) && noteState.duration() >= TDuration(DurationType::V_512TH)
                      && canUse(cr, group, actualGroup),
                      "此处时值无法使用这个连音比例；请先选定合适的音符或休止符");
            } else {
                std::vector<const ChordRest*> anchors;
                for (const auto* cr : score()->getSelectedChordRests()) {
                    if (isEligible(cr)) anchors.push_back(cr);
                }
                // The web dispatch stays one bounded undo operation even when a
                // caller pastes a very large ratio into a broad selection.
                check(!anchors.empty() && anchors.size() <= size_t(10000 / n) && std::all_of(anchors.begin(), anchors.end(),
                      [&](const auto* cr) { return canUse(cr, cr->ticks(), cr->actualTicks()); }),
                      "所选时值无法使用这个连音比例");
            }
            TupletOptions options;
            options.ratio = Fraction(n, d);
            options.autoBaseLen = shorthand;
            options.bracketType = TupletBracketType(score()->style().styleI(Sid::tupletBracketType));
            options.numberType = shorthand ? TupletNumberType(score()->style().styleI(Sid::tupletNumberType))
                                            : TupletNumberType::SHOW_RELATION; // legacy n:d clients
            if (explicitAppearance) {
                const QMap<QString, TupletNumberType> numbering {
                    {"number",TupletNumberType::SHOW_NUMBER}, {"ratio",TupletNumberType::SHOW_RELATION}, {"none",TupletNumberType::NO_TEXT}
                };
                const QMap<QString, TupletBracketType> brackets {
                    {"auto",TupletBracketType::AUTO_BRACKET}, {"show",TupletBracketType::SHOW_BRACKET}, {"hide",TupletBracketType::SHOW_NO_BRACKET}
                };
                check(numbering.contains(tokens[2]) && brackets.contains(tokens[3]), "连音显示选项无效");
                options.numberType = numbering.value(tokens[2]);
                options.bracketType = brackets.value(tokens[3]);
            }
            const auto before = undoIndex();
            isInput() ? input->addTuplet(options) : interaction->addTupletToSelectedChordRests(options);
            check(undoIndex() != before, "此处不能创建这个连音");
            return;
        }
        if (tool == "meter-rebar") {
            const QStringList parts = value.split('/');
            bool okN=false,okD=false;
            const int n=parts.value(0).toInt(&okN), d=parts.value(1).toInt(&okD);
            check(parts.size()==2 && okN && okD && n>=1 && n<=32 && d>=1 && d<=32 && !(d & (d-1)),
                  "不支持这个拍号");
            auto* measure = targetMeasure();
            check(!isInput() && measure && !measure->isMMRest(), "请选定重新划分的起始小节");
            const Fraction desired(n,d);
            check(measure->timesig() != desired, "此处无需重划小节；可直接修改拍号写法");
            const auto from=measure->tick();
            const auto scoreEnd=score()->lastMeasure()->endTick();
            const auto* stop=score()->staff(0)->nextTimeSig(from+Fraction::fromTicks(1));
            const auto until=stop ? stop->tick() : scoreEnd;
            const auto newCount=((until-from)/desired).reduced();
            check(newCount.denominator()==1 && newCount.numerator()>0 && newCount.numerator()<=128,
                  "新拍号不能整齐填满这段小节，未更改");
            bool hasNotes=false;
            int count=0;
            for (auto* m=measure; m && m->tick()<until; m=m->nextMeasure()) {
                check(++count<=32 && m->ticks()==m->timesig() && !m->irregular()
                      && !m->repeatStart() && !m->repeatEnd() && m->el().empty(),
                      "仅能重划连续的普通小节，不能跨弱起、反复或版面标记");
                check(m->noOffset()==0 && m->measureNumberMode()==MeasureNumberMode::AUTO &&
                      std::abs(m->userStretch()-1.0)<1e-9 && !m->breakMultiMeasureRest(),
                      "这段有自定义小节编号或宽度，未重划");
                if (m->endTick()<until) {
                    check(m->next() == m->nextMeasure(), "不能跨版面框重新划分");
                }
                for (const auto* seg=m->first(); seg; seg=seg->next()) {
                    for (const auto* a : seg->annotations()) {
                        check(a && (a->isDynamic() || a->isArticulationFamily()),
                              "这段有暂不能安全重划的附加记号");
                    }
                    for (const auto* e:seg->elist()) {
                        if (!e) continue;
                        const bool initial = m==measure && seg->tick()==from && (e->isClef()||e->isKeySig()||e->isTimeSig());
                        const bool boundary=e->isBarLine() && seg->tick()==m->endTick() &&
                            (toBarLine(e)->barLineType()==BarLineType::NORMAL ||
                             (m==score()->lastMeasure() && toBarLine(e)->barLineType()==BarLineType::END));
                        if (initial || boundary || (!e->isChordRest() && e->generated())) continue;
                        check(e->isChordRest() && !e->isMeasureRepeat(), "这段包含不能安全搬动的谱面元素");
                        const auto* cr=toChordRest(e);
                        check(cr->lyrics().empty() && cr->el().empty(), "这段有暂不能安全重划的附加记号");
                        if (!e->isChord()) {check(e->isRest(), "这段的节奏元素暂不支持");continue;}
                        hasNotes=true;
                        const auto* ch=toChord(e);
                        check(ch->graceNotes().empty() && !ch->arpeggio() && !ch->spanArpeggio() && !ch->tremoloSingleChord() && !ch->tremoloTwoChord(),
                              "这段有装饰音或奏法，暂不能安全重划");
                        for (const auto* note:ch->notes()) {
                            check(note && note->el().empty(), "此处有暂不能安全重划的音符标记");
                        }
                    }
                }
            }
            const auto touches=[&](const auto* span){
                return span && span->tick()<until && span->tick2()>=from;
            };
            for (const auto& pair:score()->spanner()) {
                const auto* span=pair.second;
                if (!touches(span)) continue;
                check(span->tick()>=from && span->tick2()<=until && (span->isSlur() || span->isHairpin() || span->isPedal()),
                      "这段有跨边界或暂不能安全重划的连线");
            }
            for(const auto* span:score()->unmanagedSpanners()) check(!touches(span),"这段有连线，暂不能安全重划");
            check(hasNotes,"空白小节可直接应用拍号，无需重新划分");
            // Compare sounding note identity and written spelling before and
            // after. Constituent pieces linked by fresh native ties count as
            // one note; untied repeated pitches remain separate attacks.
            using Witness=std::array<int,7>;
            auto capture=[&] {
                std::vector<Witness> result;
                for (auto* m=score()->tick2measure(from);m && m->tick()<until;m=m->nextMeasure()) {
                    for (const auto* seg=m->first();seg;seg=seg->next())
                        for (const auto* e:seg->elist()) if(e && e->isChord()) {
                            for(const auto* note:toChord(e)->notes()) {
                                if(note->tieBack())continue;
                                const Note* tail=note;
                                for(int safety=0; tail->tieFor() && safety<128; ++safety) {
                                    const auto* next=tail->tieFor()->endNote();
                                    if(!next || next->tick()<=tail->tick() || next->pitch()!=note->pitch() || next->track()!=note->track()) break;
                                    tail=next;
                                }
                                result.push_back({int(note->track()),note->pitch(),note->tpc1(),note->tpc2(),note->userVelocity(),
                                    note->tick().ticks(),(tail->tick()+tail->chord()->actualTicks()).ticks()});
                            }
                        }
                }
                std::sort(result.begin(),result.end());
                return result;
            };
            // Audit native Timeline semantics; this does not rebuild any item.
            using Mark = std::array<int,6>;
            auto marks = [&] {
                std::vector<Mark> out;
                for (const auto& kv:score()->spanner()) {
                    const auto* span=kv.second;
                    if (touches(span)) out.push_back({int(span->type()),span->subtype(),int(span->track()),int(span->track2()),
                                                     span->tick().ticks(),span->tick2().ticks()});
                }
                for (auto* m=score()->tick2measure(from);m && m->tick()<until;m=m->nextMeasure()) {
                    for (const auto* seg=m->first();seg;seg=seg->next()) {
                        for(const auto* a:seg->annotations())
                            if(a) out.push_back({int(a->type()),a->subtype(),int(a->track()),-1,a->tick().ticks(),-1});
                        for(const auto* e:seg->elist()) if(e && e->isChord()) {
                            const auto* ch=toChord(e);
                            for(const auto* a:ch->articulations())
                                if(a) out.push_back({int(a->type()),a->subtype(),int(ch->track()),-1,ch->tick().ticks(),-1});

                        }
                    }
                }
                std::sort(out.begin(),out.end());
                return out;
            };
            const auto expected=capture();
            const auto expectedMarks=marks();
            using TieAnchor=std::array<int,4>;
            auto tieAnchors=[&] {
                std::vector<TieAnchor> result;
                for(auto* m=score()->tick2measure(from);m && m->tick()<until;m=m->nextMeasure())
                    for(const auto* seg=m->first();seg;seg=seg->next())
                        for(const auto* e:seg->elist()) if(e && e->isChord())
                            for(const auto* note:toChord(e)->notes())
                                if(note->tieBack() && note->tieBack()->startNote())
                                    result.push_back({int(note->track()),note->pitch(),note->tpc1(),note->tick().ticks()});
                std::sort(result.begin(),result.end());
                return result;
            };
            const auto expectedTieAnchors=tieAnchors();
            const auto history=m_notation->undoStack();
            auto* ts=Factory::createTimeSig(score()->dummy()->segment());
            ts->setSig(desired,TimeSigType::NORMAL);
            const auto before=undoIndex();
            history->prepareChanges(muse::TranslatableString("undoableAction","Rebar measures"));
            score()->cmdAddTimeSig(measure,0,ts,false);
            const auto* changed=score()->staff(0)->timeSig(from);
            const auto* last=score()->lastMeasure();
            const auto actualTieAnchors=tieAnchors();
            const bool preserved=std::includes(actualTieAnchors.begin(),actualTieAnchors.end(),expectedTieAnchors.begin(),expectedTieAnchors.end()) && changed && changed->sig().identical(desired) && last && last->endTick()==scoreEnd
                                && capture()==expected && marks()==expectedMarks;
            if(!preserved) {
                history->rollbackChanges();
                check(false,"原音符或边界不能完整保留，已取消重划");
            }
            history->commitChanges();
            m_notation->notationChanged().notify();
            check(undoIndex()!=before,"没有可重划的小节");
            return;
        }
        if (tool == "meter-custom") {
            const auto parts = value.split('/');
            bool okN = false, okD = false;
            const int n = parts.value(0).toInt(&okN);
            const int d = parts.value(1).toInt(&okD);
            check(parts.size() == 2 && okN && okD && n >= 1 && n <= 64 && d >= 1 && d <= 64 && (d & (d-1)) == 0,
                  "拍号分子须为 1–64，分母须为 1、2、4、8、16、32 或 64");
            auto* measure = targetMeasure();
            check(!isInput() && measure && !measure->isMMRest(), "请先选中一个小节");
            const auto desired = Fraction(n,d);
            check(!measure->timesig().identical(desired), "这个小节已是该拍号");
            // Global signatures may rewrite bars until the next signature.
            // Avoid hiding that destructive operation in a custom-value control.
            const auto* stop = score()->staff(0)->nextTimeSig(measure->tick() + Fraction::fromTicks(1));
            const auto startTick = measure->tick();
            const auto endTick = stop ? stop->tick() : score()->lastMeasure()->endTick();
            const auto* signatures = measure->findSegment(SegmentType::TimeSig, startTick);
            bool durationPreserved = signatures != nullptr;
            for (staff_idx_t i = 0; durationPreserved && i < score()->nstaves(); ++i) {
                const auto* current = signatures->element(i * VOICES);
                durationPreserved = current && current->isTimeSig() &&
                    toTimeSig(current)->sig() == desired && toTimeSig(current)->stretch() == Fraction(1,1);
            }
            if (!durationPreserved) {
                for (auto* m = measure; m && m->tick() < endTick; m = m->nextMeasure()) {
                    check(m->ticks() == m->timesig() && !m->repeatStart() && !m->repeatEnd() && m->el().empty(),
                          "这段含弱起、反复或版面标记，不能直接重排拍号");
                    for (const auto* seg = m->first(); seg; seg = seg->next()) {
                        const bool atAnchor = m == measure && seg->tick() == startTick;
                        check(seg->annotations().empty(), "这段已有记号，不能直接重排拍号");
                        for (const auto* e : seg->elist()) {
                            const bool boundary = e && e->isBarLine() && seg->tick() == m->endTick() &&
                                (toBarLine(e)->barLineType() == BarLineType::NORMAL ||
                                 (m == score()->lastMeasure() && toBarLine(e)->barLineType() == BarLineType::END));
                            if (!e || boundary || (!e->isChordRest() && e->generated()) ||
                                (atAnchor && (e->isClef() || e->isKeySig() || e->isTimeSig()))) {
                                continue;
                            }
                            check(e->isRest() && (toRest(e)->durationType() == TDuration(DurationType::V_MEASURE)) && !toRest(e)->tuplet(),
                                  "已有音乐的这段不能直接重排拍号");
                        }
                    }
                }
                for (const auto& entry : score()->spanner()) {
                    const auto* spanner = entry.second;
                    check(!spanner || spanner->tick() >= endTick || spanner->tick2() < startTick,
                          "这段已有连线，不能直接重排拍号");
                }
                for (const auto* spanner : score()->unmanagedSpanners()) {
                    check(!spanner || spanner->tick() >= endTick || spanner->tick2() < startTick,
                          "这段已有连线，不能直接重排拍号");
                }
            }
            auto* meter = Factory::createTimeSig(score()->dummy()->segment());
            meter->setSig(desired, TimeSigType::NORMAL);
            const auto before = undoIndex();
            const auto history = m_notation->undoStack();
            history->prepareChanges(muse::TranslatableString("undoableAction", "Custom time signature"));
            score()->cmdAddTimeSig(measure, 0, meter, false);
            history->commitChanges();
            m_notation->notationChanged().notify();
            check(undoIndex() != before, "此处不能改为这个拍号");
            return;
        }

        if (tool == "hairpin") {
            const QMap<QString, HairpinType> presets {
                {"cresc-line",HairpinType::CRESC_LINE}, {"dim-line",HairpinType::DIM_LINE},
                {"cresc",HairpinType::CRESC_HAIRPIN}, {"dim",HairpinType::DIM_HAIRPIN}
            };
            check(presets.contains(value), "unsupported hairpin");
            auto& selected = score()->selection();
            check(!selected.isNone() && !score()->getSelectedChordRests().empty(), "请先选择音符、休止符或乐句");
            if (selected.isRange() && selected.staffStart() + 1 < selected.staffEnd()) {
                for (auto staff = selected.staffStart(); staff < selected.staffEnd(); ++staff) {
                    check(selected.firstChordRest(staff * VOICES) != nullptr,
                          "每个选中的谱表都需要一个可用的节奏位置");
                }
            }
            const auto before = undoIndex();
            interaction->addHairpinsToSelection(presets.value(value));
            check(undoIndex() != before, "此位置不能应用力度渐变");
            return;
        }
        // PaletteCreator and PaletteProvider use these same upstream kinds and
        // applyPaletteElement. The prototype is never inserted directly.
        auto* selected = interaction->selection()->element();
        const bool rhythmic = selected && (selected->isNote() || selected->isChordRest());
        if (tool == "articulation" || tool == "flip" || tool == "layout") {
            const auto before = undoIndex();
            if (tool == "articulation") {
                const QMap<QString, SymId> presets {
                    {"accent",SymId::articAccentAbove}, {"staccato",SymId::articStaccatoAbove},
                    {"tenuto",SymId::articTenutoAbove}, {"marcato",SymId::articMarcatoAbove}
                };
                check(presets.contains(value) && (isInput() || (rhythmic && !selected->isRest())),
                      "select a note or enter note input for this articulation");
                if (isInput()) {
                    input->setArticulation(presets.value(value));
                    return;
                }
                interaction->toggleArticulationForSelection(presets.value(value));
            } else if (tool == "flip") {
                check(!isInput() && rhythmic, "select a note or rest to flip");
                interaction->flipSelection();
            } else {
                check(!isInput() && selected && selected->findMeasure() && interaction->toggleLayoutBreakAvailable(),
                      "select a measure anchor to add a layout break");
                const QMap<QString, LayoutBreakType> presets {
                    {"line",LayoutBreakType::LINE}, {"page",LayoutBreakType::PAGE}, {"section",LayoutBreakType::SECTION}
                };
                check(presets.contains(value), "unsupported layout break");
                interaction->toggleLayoutBreak(presets.value(value));
            }
            check(undoIndex() != before, "no change at this selection");
            return;
        }
        check(!isInput() && selected && (rhythmic || (tool == "barline" && selected->isBarLine())),
              "请退出音符输入并选中要应用符号的音符或休止符");
        auto* nativeScore = score();
        auto* dummySegment = nativeScore->dummy()->segment();
        std::shared_ptr<EngravingItem> item;
        if (tool == "clef") {
            const QMap<QString, ClefType> presets {
                {"treble", ClefType::G}, {"treble8va", ClefType::G8_VA}, {"treble15ma", ClefType::G15_MA},
                {"treble8vb", ClefType::G8_VB}, {"alto", ClefType::C3}, {"tenor", ClefType::C4},
                {"bass", ClefType::F}, {"bass8va", ClefType::F_8VA}, {"bass8vb", ClefType::F8_VB}
            };
            check(presets.contains(value), "unsupported clef");
            const auto type = presets.value(value);
            auto clef = Factory::makeClef(dummySegment);
            clef->setClefType(ClefTypeList(type, type));
            item = std::move(clef);
        } else if (tool == "key") {
            // Concert-key fifths, not a mode or staff-transposition change.
            const QStringList presets {"Cb", "Gb", "Db", "Ab", "Eb", "Bb", "F",
                                      "C", "G", "D", "A", "E", "B", "F#", "C#"};
            const auto index = presets.indexOf(value);
            check(index >= 0, "unsupported key");
            auto key = Factory::makeKeySig(dummySegment);
            key->setKey(Key(int(index) - 7));
            item = std::move(key);
        } else if (tool == "meter") {
            const QStringList meters {"2/4","3/4","4/4","5/4","6/4","3/8","4/8","5/8",
                                     "6/8","7/8","9/8","12/8","2/2","3/2"};
            const auto type = value == "common" ? TimeSigType::FOUR_FOUR
                             : value == "cut" ? TimeSigType::ALLA_BREVE : TimeSigType::NORMAL;
            check(type != TimeSigType::NORMAL || meters.contains(value), "unsupported meter");
            const auto numeric = (type == TimeSigType::FOUR_FOUR) ? QString("4/4")
                               : (type == TimeSigType::ALLA_BREVE) ? QString("2/2") : value;
            auto meter = Factory::makeTimeSig(dummySegment);
            meter->setSig(Fraction(numeric.section('/', 0, 0).toInt(), numeric.section('/', 1, 1).toInt()), type);
            item = std::move(meter);
        } else if (tool == "tempo") {
            const QMap<QString, double> words {
                {"Grave",35}, {"Largo",50}, {"Lento",52.5}, {"Larghetto",63}, {"Adagio",71},
                {"Andante",92}, {"Andantino",94}, {"Moderato",114}, {"Allegretto",116}, {"Allegro",144},
                {"Vivace",172}, {"Presto",187}, {"Prestissimo",200}
            };
            const QMap<QString, QPair<QString,double>> notes {
                {"h", {"metNoteHalfUp",2}}, {"q", {"metNoteQuarterUp",1}}, {"e", {"metNote8thUp",0.5}},
                {"h.", {"metNoteHalfUp",3}}, {"q.", {"metNoteQuarterUp",1.5}}, {"e.", {"metNote8thUp",0.75}}
            };
            auto tempo = std::make_shared<TempoText>(dummySegment);
            if (words.contains(value)) {
                tempo->setFollowText(false);
                tempo->setXmlText(muse::String::fromQString(value));
                tempo->setTempo(BeatsPerSecond(words.value(value) / 60.0));
            } else {
                const auto unit = value.contains(':') ? value.section(':', 0, 0) : QString("q");
                const auto count = value.contains(':') ? value.section(':', 1) : value;
                bool ok = false;
                const auto bpm = count.toInt(&ok);
                check(ok && bpm >= 20 && bpm <= 400 && QString::number(bpm) == count && notes.contains(unit),
                      "unsupported tempo");
                const auto [symbol, factor] = notes.value(unit);
                const auto dot = unit.endsWith('.') ? QString("<sym>space</sym><sym>metAugmentationDot</sym>") : QString();
                tempo->setFollowText(true);
                tempo->setXmlText(muse::String::fromQString("<sym>" + symbol + "</sym>" + dot + " = " + count));
                tempo->setTempo(BeatsPerSecond(bpm * factor / 60.0));
            }
            item = std::move(tempo);
        } else if (tool == "dynamics") {
            const QStringList presets {"ppp","pp","p","mp","mf","f","ff","fff","fp","pf","sf","sfz","sff",
                                       "sffz","sfp","rfz","rf","fz"};
            check(presets.contains(value), "unsupported dynamic");
            auto dynamic = std::make_shared<Dynamic>(dummySegment);
            dynamic->setDynamicType(muse::String::fromQString(value));
            item = std::move(dynamic);
        } else if (tool == "arpeggio") {
            const QMap<QString, ArpeggioType> presets {
                {"normal",ArpeggioType::NORMAL}, {"up",ArpeggioType::UP}, {"down",ArpeggioType::DOWN},
                {"bracket",ArpeggioType::BRACKET}, {"up-straight",ArpeggioType::UP_STRAIGHT},
                {"down-straight",ArpeggioType::DOWN_STRAIGHT}
            };
            check(presets.contains(value) && selected->isNote() && toNote(selected)->chord()->notes().size() > 1,
                  "select a notehead in a chord for an arpeggio");
            auto arpeggio = Factory::makeArpeggio(nativeScore->dummy()->chord());
            arpeggio->setArpeggioType(presets.value(value));
            item = std::move(arpeggio);
        } else if (tool == "barline") {
            const QMap<QString, BarLineType> presets {
                {"normal",BarLineType::NORMAL}, {"double",BarLineType::DOUBLE},
                {"start-repeat",BarLineType::START_REPEAT}, {"end-repeat",BarLineType::END_REPEAT},
                {"end-start-repeat",BarLineType::END_START_REPEAT}, {"dashed",BarLineType::BROKEN},
                {"final",BarLineType::END}, {"dotted",BarLineType::DOTTED},
                {"reverse-final",BarLineType::REVERSE_END}, {"heavy",BarLineType::HEAVY},
                {"double-heavy",BarLineType::DOUBLE_HEAVY}
            };
            check(presets.contains(value), "unsupported barline");
            const auto type = presets.value(value);
            const bool repeat = (type & (BarLineType::START_REPEAT | BarLineType::END_REPEAT | BarLineType::END_START_REPEAT));
            check(!repeat || selected->isBarLine(), "select a barline to place a repeat");
            auto barline = Factory::makeBarLine(dummySegment);
            barline->setBarLineType(type);
            item = std::move(barline);
        } else {
            check(false, "unsupported native score tool");
        }
        const auto before = undoIndex();
        check(interaction->applyPaletteElement(item.get()) && undoIndex() != before, "此位置不能应用该符号");
    }

    bool duplicateAt(const QString& action, const QJsonObject& body)
    {
        using namespace mu::engraving;
        check(m_editable && !isInput(), "exit note input before duplicating score material");
        const auto interaction = m_notation->interaction();
        const auto selection = interaction->selection();
        auto point = [&](const char* xKey, const char* yKey) {
            const qreal x = body.value(xKey).toDouble(-1);
            const qreal y = body.value(yKey).toDouble(-1);
            check(std::isfinite(x) && std::isfinite(y) && x >= 0 && x < 960 && y >= 0 && y < 1800,
                  "invalid pointer position");
            const auto pos = view().inverted().map(QPointF(x, y));
            return muse::PointF(pos.x(), pos.y());
        };
        const auto target = point("x", "y");
        const auto before = undoIndex();
        if (action == "range-copy-at") {
            check(selection->isRange() && bool(selection->canCopy()), "select a copyable range first");
            const auto range = selection->range();
            const auto* hit = interaction->hitElement(target, 5.f);
            if ((!hit || hit->isMeasure()) && range->containsPoint(target)) {
                return false;  // selected blank area needs a real outgoing drag
            }
            const auto source = range->startTick();
            const auto length = range->endTick() - source;
            const auto first = range->startStaffIndex();
            const auto span = range->endStaffIndex() - first;
            const auto data = selection->mimeData();
            check(!data.empty() && interaction->startDropRange(source, length, first, span, true),
                  "selection cannot be duplicated");
            // Scope these transient native drop targets to one message.
            const bool ok = interaction->updateDropRange(target) &&
                            interaction->dropRange(data.toQByteArrayNoCopy(), target, false);
            interaction->endDrop();
            check(ok, "passage does not fit at this position");
        } else {
            const auto source = point("sourceX", "sourceY");
            const auto hits = interaction->hitElements(source, 1.f);
            const auto chosen = std::find_if(hits.rbegin(), hits.rend(), [&](const EngravingItem* item) {
                // Only score-local attachments are safe to drag-clone in this worker.
                return (item->isDynamic() || item->isArticulationFamily() || item->isTempoText()
                        || item->isKeySig() || item->isTimeSig() || item->isClef())
                       && interaction->isOutgoingDragElementAllowed(item);
            });
            check(chosen != hits.rend(), "select a supported marking to duplicate");
            const auto data = (*chosen)->mimeData();
            check(!data.empty() && interaction->startDropSingle(data.toQByteArrayNoCopy()), "marking cannot be duplicated");
            const bool ok = interaction->updateDropSingle(target, {}) && interaction->dropSingle(target, {});
            interaction->endDrop();
            check(ok, "marking does not fit at this position");
        }
        check(undoIndex() != before, "no change at this position");
        return true;
    }

    mu::engraving::Measure* targetMeasure() const
    {
        const auto selection = m_notation->interaction()->selection();
        if (selection->isRange()) {
            const auto mm = selection->range()->measureRange();
            return mm.startMeasure == mm.endMeasure ? const_cast<mu::engraving::Measure*>(mm.startMeasure) : nullptr;
        }
        auto* item = selection->element();
        return item ? item->findMeasure() : nullptr;
    }

    bool safeMeasureLength(const mu::engraving::Measure* measure, const mu::engraving::Fraction& desired) const
    {
        using namespace mu::engraving;
        if (!measure || desired <= Fraction(0,1) || desired > Fraction(8,1) || measure->isMMRest()) {
            return false;
        }
        if (desired >= measure->ticks()) {
            return true;
        }
        const auto reduction = measure->ticks() - desired;
        const auto cutoff = measure->tick() + desired;
        // MuseScore's adjustToLen consumes trailing rhythmic elements, removes
        // associated segment annotations, and may change or delete spanners.
        // Refuse if this would trim anything but plain trailing rests.
        for (const auto* seg = measure->first(); seg; seg = seg->next()) {
            if (seg->tick() >= cutoff && (!seg->annotations().empty() || seg->segmentType() == SegmentType::Breath)) {
                return false;
            }
            for (const auto* item : seg->elist()) {
                if (item && item->isChord() && item->tick() + toChord(item)->actualTicks() > cutoff) {
                    return false;
                }
                if (item && item->isMeasureRepeat()) {
                    return false;
                }
            }
        }
        for (const auto& entry : score()->spanner()) {
            const auto* spanner = entry.second;
            if (spanner && spanner->tick() < measure->endTick() && spanner->tick2() >= measure->tick()) {
                return false;
            }
        }
        for (const auto* spanner : score()->unmanagedSpanners()) {
            if (spanner && spanner->tick() < measure->endTick() && spanner->tick2() >= measure->tick()) {
                return false;
            }
        }
        for (track_idx_t track = 0; track < score()->nstaves() * VOICES; ++track) {
            const auto firstTrack = track / VOICES * VOICES;
            int restCount = 0;
            bool chordFound = false;
            for (const auto* seg = measure->first(); seg; seg = seg->next()) {
                for (track_idx_t lane = firstTrack; lane < firstTrack + VOICES; ++lane) {
                    const auto* e = seg->element(lane);
                    restCount += e && e->isRest();
                    chordFound = chordFound || (e && e->isChord());
                }
            }
            // Upstream keeps one unaccompanied measure rest on a staff and
            // only rewrites that rest; no annotation scan occurs on that staff.
            if (restCount == 1 && !chordFound) {
                continue;
            }
            bool populated = false;
            auto remaining = reduction;
            for (const auto* seg = measure->last(); seg; seg = seg->prev()) {
                if (seg->isChordRestType() &&
                    std::any_of(seg->annotations().begin(), seg->annotations().end(),
                                [track](const EngravingItem* item) { return item && item->track() == track; })) {
                    return false;
                }
                const auto* item = seg->element(track);
                if (item && seg->segmentType() == SegmentType::Breath) {
                    return false;
                }
                if (!item || !item->isChordRest()) {
                    continue;
                }
                populated = true;
                const auto* cr = toChordRest(item);
                if (!cr->isRest() || cr->tuplet() || cr->actualTicks() <= Fraction(0,1)) {
                    break;
                }
                remaining -= cr->actualTicks();
                if (remaining <= Fraction(0,1)) {
                    break;
                }
            }
            if (populated && remaining > Fraction(0,1)) {
                return false;
            }
        }
        return true;
    }

    void editMeasureLength(const QString& action, const QJsonObject& body)
    {
        using namespace mu::engraving;
        check(m_editable && !isInput(), "exit note input before editing a measure");
        auto* measure = targetMeasure();
        check(measure != nullptr, "select one measure");
        const bool reset = action == "measure-length-reset";
        Fraction desired = measure->timesig();
        const auto value = body.value("value").toArray();
        if (!reset) {
            const int n = value.size() == 2 ? value.at(0).toInt(0) : 0;
            const int d = value.size() == 2 ? value.at(1).toInt(0) : 0;
            check(n >= 1 && n <= 64 && d >= 1 && d <= 64 && (d & (d-1)) == 0,
                  "invalid written measure duration");
            desired = Fraction(n,d);
        }
        const auto excluded = body.value("excludeNumber");
        check(excluded.isUndefined() || excluded.isBool(), "invalid measure count option");
        check(safeMeasureLength(measure, desired), "缩短会移除音符或记号，先处理要裁掉的部分");
        if (desired == measure->ticks() && (excluded.isUndefined() || excluded.toBool() == measure->irregular())) {
            return;
        }
        const auto history = m_notation->undoStack();
        history->prepareChanges(muse::TranslatableString("undoableAction", "Edit measure duration"));
        if (desired != measure->ticks()) {
            measure->adjustToLen(desired);
        }
        if (!excluded.isUndefined()) {
            measure->undoChangeProperty(Pid::IRREGULAR, excluded.toBool());
        }
        history->commitChanges();
        m_notation->notationChanged().notify();
    }

    QJsonObject measureState(bool showLengthOptions = false) const
    {
        const auto selection = m_notation->interaction()->selection();
        QJsonObject result;
        if (selection->isRange()) {
            const auto r = selection->range();
            const auto mm = r->measureRange();
            if (mm.startMeasure && mm.endMeasure) {
                const bool whole = r->startTick() == mm.startMeasure->tick() && r->endTick() == mm.endMeasure->endTick();
                result.insert("whole", whole);
                result.insert("canJoin", whole && mm.startMeasure != mm.endMeasure);
                result.insert("canDelete", whole && (mm.startMeasure != score()->firstMeasure() || mm.endMeasure != score()->lastMeasure()));
            }
        }
        if (auto* measure = targetMeasure()) {
            using namespace mu::engraving;
            result.insert("number", 1 + measure->no());
            QJsonArray staves;
            for (staff_idx_t staff = 0; staff < score()->nstaves(); ++staff) {
                staves.append(QJsonObject{{"visible", measure->visible(staff)}, {"stemless", measure->stemless(staff)}});
            }
            result.insert("staves", staves);
            result.insert("repeatCount", measure->repeatCount());
            result.insert("repeatEnd", measure->repeatEnd());
            result.insert("previousMeasure", bool(adjacentPropertiesMeasure(false)));
            result.insert("nextMeasure", bool(adjacentPropertiesMeasure(true)));
            result.insert("breakMMR", measure->breakMultiMeasureRest());
            result.insert("numberMode", int(measure->measureNumberMode()));
            result.insert("excludeNumber", measure->irregular());
            result.insert("numberOffset", measure->noOffset());
            result.insert("stretch", measure->userStretch());
            result.insert("nominal", QJsonArray{measure->timesig().numerator(), measure->timesig().denominator()});
            result.insert("actual", QJsonArray{measure->ticks().numerator(), measure->ticks().denominator()});
            const auto* item = selection->element();
            const bool first = measure == score()->firstMeasure();
            result.insert("first", first);
            if (showLengthOptions) {
                const auto nominal = measure->timesig();
                QJsonArray pickups;
                for (int beats = 1; beats <= 4 && beats < nominal.numerator(); ++beats) {
                    const Fraction value(beats, nominal.denominator());
                    if (value != measure->ticks() && safeMeasureLength(measure, value)) {
                        pickups.append(QJsonArray{beats, nominal.denominator()});
                    }
                }
                result.insert("pickupOptions", pickups);
            }
            result.insert("canResetLength", showLengthOptions && measure->ticks() != measure->timesig() && safeMeasureLength(measure, measure->timesig()));
            result.insert("canSplit", item && (item->isNote() || item->isRest()) && item->tick() > measure->tick());
        }
        return result;
    }

    QString timeDeleteKind() const
    {
        using namespace mu::engraving;
        if (!editable() || isInput() || !score()->firstMeasure() || !score()->lastMeasure()) return {};
        const auto& sel=score()->selection();
        const auto from=score()->firstMeasure()->tick(), until=score()->lastMeasure()->endTick();
        if (sel.isRange()) return sel.tickStart()>from || sel.tickEnd()<until ? QStringLiteral("range") : QString();
        const auto* el=sel.element();
        if (!el) return {};
        if (el->isBarLine()) {
            const auto* seg=toBarLine(el)->segment();
            return seg && seg->isEndBarLineType() && seg->measure()->nextMeasure() ? QStringLiteral("join") : QString();
        }
        const auto* cr=el->isNote() ? static_cast<const ChordRest*>(toNote(el)->chord()) : el->isChordRest() ? toChordRest(el) : nullptr;
        if (!cr || !cr->segment()) return {};
        const auto start=cr->segment()->tick(), end=start+cr->actualTicks();
        if (end<=start || end>until || (start<=from && end==until)) return {};
        if (end==until) return QStringLiteral("rhythm");
        const auto* endMeasure=score()->tick2measure(end);
        // Stock passes a missing end segment as "until end of score".
        // Refuse ambiguous offsets rather than truncating unrelated music.
        return endMeasure && endMeasure->findSegment(SegmentType::ChordRest,end) ? QStringLiteral("rhythm") : QString();
    }

    mu::engraving::Measure* adjacentPropertiesMeasure(bool next) const
    {
        auto* m=targetMeasure();
        if (!m) return nullptr;
        while ((m=next?m->nextMeasureMM():m->prevMeasureMM())) {
            if (!m->isMMRest()) return m;
        }
        return nullptr;
    }

    void stepMeasureProperties(const QJsonObject& values,const QString& direction)
    {
        using namespace mu::engraving;
        check(direction=="next" || direction=="previous","invalid measure direction");
        auto* m=adjacentPropertiesMeasure(direction=="next");
        check(m,"no adjacent measure");
        editMeasureProperties(values);
        auto interaction=m_notation->interaction();
        interaction->select({m}, SelectType::SINGLE, 0);
        if (m->system()) {
            const auto& pages=m_notation->elements()->pages();
            const auto p=std::find(pages.begin(),pages.end(),m->system()->page());
            if (p!=pages.end()) setPage(int(std::distance(pages.begin(),p)));
        }
    }

    void editMeasureProperties(const QJsonObject& values)
    {
        using namespace mu::engraving;
        check(editable() && !isInput(), "exit note input before editing a measure");
        auto* measure = targetMeasure();
        check(measure != nullptr, "select exactly one measure");
        const auto actual = values.value("actual").toArray();
        const auto numerator = actual.size()==2 ? actual.at(0).toInt(0) : 0;
        const auto denominator = actual.size()==2 ? actual.at(1).toInt(0) : 0;
        check(numerator >= 1 && numerator <= 64 && denominator >= 1 && denominator <= 64
                  && (denominator & (denominator - 1)) == 0, "invalid actual measure duration");
        const Fraction desired(numerator, denominator);
        const auto mode = values.value("numberMode").toInt(-1);
        const auto offset = values.value("numberOffset").toInt(-1000);
        const auto count = values.value("repeatCount").toInt(0);
        const auto stretch = values.value("stretch").toDouble(-1);
        check(mode >= 0 && mode <= 2 && offset >= -999 && offset <= 999 && count >= 1 && count <= 9999
                  && std::isfinite(stretch) && stretch >= .25 && stretch <= 4.
                  && values.value("excludeNumber").isBool() && values.value("breakMMR").isBool(),
              "invalid measure properties");
        const auto staves = values.value("staves").toArray();
        check(staves.size() == static_cast<qsizetype>(score()->nstaves()), "invalid measure staff scope");
        for (const auto v : staves) {
            const auto obj = v.toObject();
            check(obj.value("visible").isBool() && obj.value("stemless").isBool(), "invalid measure staff settings");
        }
        if (desired != measure->ticks()) {
            check(safeMeasureLength(measure, desired), "shortening would remove musical content");
        }
        const auto history = m_notation->undoStack();
        history->prepareChanges(muse::TranslatableString("undoableAction", "Edit measure properties"));
        try {
            bool changed = false;
            for (staff_idx_t staff = 0; staff < score()->nstaves(); ++staff) {
                const auto opt = staves.at(static_cast<qsizetype>(staff)).toObject();
                const bool visible = opt.value("visible").toBool(), stemless = opt.value("stemless").toBool();
                if (visible != measure->visible(staff) || stemless != measure->stemless(staff)) {
                    score()->undo(new ChangeMStaffProperties(measure, static_cast<int>(staff), visible, stemless));
                    changed = true;
                }
            }
            const auto set = [&](Pid prop, const PropertyValue& desired) {
                if (measure->getProperty(prop) != desired) {
                    measure->undoChangeProperty(prop, desired);
                    changed = true;
                }
            };
            if (measure->repeatEnd()) set(Pid::REPEAT_COUNT, count);
            set(Pid::BREAK_MMR, values.value("breakMMR").toBool());
            set(Pid::USER_STRETCH, stretch);
            set(Pid::MEASURE_NUMBER_MODE, mode);
            set(Pid::NO_OFFSET, offset);
            set(Pid::IRREGULAR, values.value("excludeNumber").toBool());
            if (desired != measure->ticks()) {
                measure->adjustToLen(desired);
                changed = true;
            }
            if (changed) measure->triggerLayout();
            history->commitChanges();
        } catch (...) {
            history->rollbackChanges();
            throw;
        }
        m_notation->notationChanged().notify();
    }
    void editMeasureProperty(const QString& action, const QJsonValue& value)
    {
        using namespace mu::engraving;
        check(m_editable && !isInput(), "exit note input before editing a measure");
        auto* measure = targetMeasure();
        check(measure != nullptr, "select one measure");
        Pid pid = Pid::END;
        PropertyValue newValue;
        if (action == "measure-number-mode") {
            const int mode = value.toInt(-1);
            check(mode >= 0 && mode <= 2, "invalid measure numbering mode");
            pid = Pid::MEASURE_NUMBER_MODE;
            newValue = mode;
        } else if (action == "measure-exclude-number") {
            check(value.isBool(), "invalid measure count option");
            pid = Pid::IRREGULAR;
            newValue = value.toBool();
        } else if (action == "measure-number-offset") {
            const int n = value.toInt(-1001);
            check(n >= -999 && n <= 999, "invalid measure number offset");
            pid = Pid::NO_OFFSET;
            newValue = n;
        } else if (action == "measure-stretch") {
            const double stretch = value.toDouble(-1.);
            check(std::isfinite(stretch) && stretch >= 0.25 && stretch <= 4., "invalid measure stretch");
            pid = Pid::USER_STRETCH;
            newValue = stretch;
        }
        check(pid != Pid::END, "unsupported measure property");
        if (measure->getProperty(pid) == newValue) {
            return;
        }
        const auto history = m_notation->undoStack();
        history->prepareChanges(muse::TranslatableString("undoableAction", "Edit measure properties"));
        measure->undoChangeProperty(pid, newValue);
        history->commitChanges();
        m_notation->notationChanged().notify();
    }

    bool clipboardReady() const { return bool(m_scoreClipboard); }

    void editContent(const QString& action, int count)
    {
        using namespace mu::engraving;
        using namespace mu::notation;
        check(m_editable && (!isInput() || action == "paste-special"), "exit note input before editing a passage");
        const auto interaction = m_notation->interaction();
        const auto selection = interaction->selection();
        const auto before = undoIndex();
        if (action == "copy" || action == "cut") {
            check(selection->canCopy(), "selection cannot be copied");
            std::unique_ptr<QMimeData> next(selection->qMimeData());
            check(next && !next->formats().isEmpty(), "selection cannot be copied");
            const auto& engravingSelection = score()->selection();
            const auto staves = selection->isRange() ? engravingSelection.staffEnd() - engravingSelection.staffStart() : 0;
            const auto span = selection->isRange() ? engravingSelection.tickEnd() - engravingSelection.tickStart() : Fraction();
            if (action == "cut") {
                interaction->deleteSelection();
                check(undoIndex() != before, "nothing to cut");
            }
            m_clipboardStaves = staves;
            m_clipboardSpan = span;
            m_scoreClipboard = std::move(next);
            return;
        }

        if (action == "repeat-selection") {
            const auto result = interaction->repeatSelection();
            if (!result) throw std::runtime_error(result.toString());
            check(undoIndex() != before, "selection cannot be repeated at the end of this score");
            return;
        }
        if (action == "paste" || action == "paste-half" || action == "paste-double"
            || action == "paste-special" || action == "swap-selection") {
            check(m_scoreClipboard != nullptr, "score clipboard is empty");
            check(!selection->isNone(), "select the first note or rest of the destination");
            Fraction scale(1, 1);
            if (action == "paste-half") scale = Fraction(1, 2);
            else if (action == "paste-double") scale = Fraction(2, 1);
            else if (action == "paste-special") {
                const auto duration = interaction->noteInput()->state().duration().fraction();
                if (duration.isValid() && !duration.isZero()) {
                    scale = duration * 4;
                    scale.reduce();
                }
            }
            std::unique_ptr<QMimeData> previousSelection;
            staff_idx_t previousStaves = 0;
            Fraction previousSpan;
            if (action == "swap-selection") {
                check(selection->canCopy(), "destination cannot be copied");
                previousSelection.reset(selection->qMimeData());
                check(previousSelection && !previousSelection->formats().isEmpty(), "destination cannot be copied");
                if (selection->isRange()) {
                    const auto& rng = score()->selection();
                    previousStaves = rng.staffEnd() - rng.staffStart();
                    previousSpan = rng.tickEnd() - rng.tickStart();
                }
            }
            if (m_clipboardStaves) {
                const auto& engravingSelection = score()->selection();
                const auto* target = selection->isRange() ? engravingSelection.firstChordRest() : selection->element();
                check(target && (target->isNote() || target->isChordRest()), "select the first note or rest of the destination");
                check(target->staffIdx() + m_clipboardStaves <= score()->nstaves(),
                      "passage would not fit the destination staves");
                check(score()->lastMeasure() && target->tick() + m_clipboardSpan * scale <= score()->lastMeasure()->endTick(),
                      "passage would not fit through the end of the score");
            }
            QMimeDataAdapter data(m_scoreClipboard.get());
            const auto history = m_notation->undoStack();
            history->prepareChanges(muse::TranslatableString("undoableAction", "Paste"));
            try {
                score()->cmdPaste(&data, nullptr, scale);
                history->commitChanges();
            } catch (...) {
                history->rollbackChanges();
                throw;
            }
            if (previousSelection && undoIndex() != before) {
                m_clipboardStaves = previousStaves;
                m_clipboardSpan = previousSpan;
                m_scoreClipboard = std::move(previousSelection);
            }
        } else if (action == "delete") {
            check(!selection->isNone(), "select notes or a passage to clear");
            interaction->deleteSelection();
        } else if (action == "time-delete") {
            check(!timeDeleteKind().isEmpty(),"select a bounded duration or internal barline");
            interaction->removeSelectedRange();
        } else if (action == "remove-empty-tail") {
            auto* trailing = score()->firstTrailingMeasure();
            if (!trailing) return; // identical to the upstream no-op
            check(trailing != score()->firstMeasure(), "keep at least one measure in the score");
            interaction->removeEmptyTrailingMeasures();
        } else if (action == "measure-delete") {
            check(selection->isRange(), "select a measure or a range of measures");
            const auto mm = selection->range()->measureRange();
            check(mm.startMeasure && mm.endMeasure &&
                  (mm.startMeasure != score()->firstMeasure() || mm.endMeasure != score()->lastMeasure()),
                  "keep at least one measure in the score");
            interaction->removeSelectedMeasures();
        } else if (action == "measure-split") {
            auto* item = selection->element();
            check(item && (item->isNote() || item->isRest()) && item->findMeasure() &&
                  item->tick() > item->findMeasure()->tick(), "select a note or rest inside the measure");
            interaction->splitSelectedMeasure();
        } else if (action == "measure-join") {
            check(selection->isRange(), "select the measures to join");
            const auto mm = selection->range()->measureRange();
            check(mm.startMeasure && mm.endMeasure && mm.startMeasure != mm.endMeasure, "select at least two measures to join");
            interaction->joinSelectedMeasures();
        } else {
            const QMap<QString, AddBoxesTarget> locations {
                {"measure-add-before",AddBoxesTarget::BeforeSelection},
                {"measure-add-after",AddBoxesTarget::AfterSelection},
                {"measure-add-start",AddBoxesTarget::AtStartOfScore},
                {"measure-add-end",AddBoxesTarget::AtEndOfScore}
            };
            check(locations.contains(action) && count >= 1 && count <= 64, "unsupported measure insertion");
            const auto location = locations.value(action);
            if (location == AddBoxesTarget::BeforeSelection || location == AddBoxesTarget::AfterSelection) {
                check(!selection->isNone() && bool(interaction->canAddBoxes()), "select a measure to insert beside");
            }
            interaction->addBoxes(BoxType::Measure, count, location);
        }
        check(undoIndex() != before, "no change at this selection");
    }

    bool editable() const { return m_editable; }
    bool canUndo() const { return m_notation->undoStack()->canUndo(); }
    bool canRedo() const { return m_notation->undoStack()->canRedo(); }
    QString selectionKind() const
    {
        const auto selection = m_notation->interaction()->selection();
        if (selection->isRange()) {
            return "Range";
        }
        const auto* item = selection->element();
        if (!item) {
            return {};
        }
        return item->isBarLine() ? "BarLine" : item->isNote() ? "Note" : item->isRest() ? "Rest" : "Other";
    }

    int selectedPitch() const
    {
        auto* item = m_notation->interaction()->selection()->element();
        return item && item->isNote() ? mu::engraving::toNote(item)->pitch() : -1;
    }

    void changePitch(bool up = true, bool octave = true)
    {
        check(m_editable, "cannot change pitch in read-only mode");
        check(!m_notation->interaction()->selection()->notes().empty(), "请先选择要改音高的音符");
        using namespace mu::notation;
        m_notation->interaction()->movePitch(up ? MoveDirection::Up : MoveDirection::Down,
                                             octave ? PitchMode::OCTAVE : PitchMode::CHROMATIC);
    }

    void undo()
    {
        check(m_editable && m_notation->undoStack()->canUndo(), "native undo unavailable");
        m_notation->interaction()->undo();
    }

    void redo()
    {
        check(m_editable && m_notation->undoStack()->canRedo(), "native redo unavailable");
        m_notation->interaction()->redo();
    }

    void clearSelection() { m_notation->interaction()->clearSelection(); }

    void save(const QString& output)
    {
        const muse::Ret result = m_project->save(output, mu::project::SaveMode::SaveAs, false);
        if (!result) {
            throw std::runtime_error(result.toString());
        }
    }

private:
    // Native SLine keeps inactive segments in a reuse pool. A failed/cancelled
    // extension must not leak a second, temporarily edited segment into MSCZ,
    // nor leave user-authored geometry behind on a recycled segment. Snapshot
    // topology as well as authored properties, in the existing undo macro.
    class RestoreOttavaGeometry final : public mu::engraving::UndoCommand
    {
    public:
        using Item = mu::engraving::SpannerSegment;
        using Property = mu::engraving::Pid;
        using Value = mu::engraving::PropertyValue;
        using Flags = mu::engraving::PropertyFlags;
        struct Entry { Item* item; Property id; Value value; Flags flags; };
        struct Geometry {
            std::vector<Item*> active;
            std::vector<Item*> idle;
            std::vector<Entry> properties;
        };
        struct Edit {
            mu::engraving::Spanner* spanner;
            mu::engraving::Fraction first;
            mu::engraving::Fraction last;
            Geometry original;
            Geometry final;
        };
        explicit RestoreOttavaGeometry(mu::engraving::Spanner* sp)
            : m_edit(std::make_shared<Edit>())
        {
            m_edit->spanner = sp;
            m_edit->first = sp->tick();
            m_edit->last = sp->tick2();
            m_edit->original = snapshot(sp);
        }
        RestoreOttavaGeometry(std::shared_ptr<Edit> edit, bool redoBoundary)
            : m_edit(std::move(edit)), m_redoBoundary(redoBoundary) {}
        void includeCurrentCoverage()
        {
            m_edit->first = std::min(m_edit->first, m_edit->spanner->tick());
            m_edit->last = std::max(m_edit->last, m_edit->spanner->tick2());
        }
        RestoreOttavaGeometry* after()
        {
            m_edit->final = snapshot(m_edit->spanner);
            return new RestoreOttavaGeometry(m_edit, true);
        }
        const char* name() const override { return "RestoreOttavaGeometry"; }
        void undo(mu::engraving::EditData*) override { if (!m_redoBoundary) restore(m_edit->original); }
        void redo(mu::engraving::EditData*) override { if (m_redoBoundary) restore(m_edit->final); }
    private:
        static constexpr std::array<Property, 5> geometryProperties{
            Property::OFFSET, Property::OFFSET2, Property::MIN_DISTANCE, Property::AUTOPLACE, Property::GENERATED
        };
        static std::vector<Item*> drainIdle(mu::engraving::Spanner* sp)
        {
            std::vector<Item*> result;
            while (auto* part = sp->popUnusedSegment()) result.push_back(part);
            return result;
        }
        static Geometry snapshot(mu::engraving::Spanner* sp)
        {
            Geometry g;
            g.active = sp->spannerSegments();
            g.idle = drainIdle(sp);
            const auto capture = [&](Item* part) {
                for (const auto id : geometryProperties) {
                    g.properties.push_back({part, id, part->getProperty(id), part->propertyFlags(id)});
                }
            };
            for (auto* part : g.active) capture(part);
            for (auto* part : g.idle) {
                capture(part);
                sp->pushUnusedSegment(part);
            }
            return g;
        }
        void restore(const Geometry& g)
        {
            auto* sp = m_edit->spanner;
            auto inventory = sp->spannerSegments();
            const auto idle = drainIdle(sp);
            inventory.insert(inventory.end(), idle.begin(), idle.end());
            const auto tracked = [&](const Item* part) {
                return std::find(g.active.begin(), g.active.end(), part) != g.active.end() ||
                       std::find(g.idle.begin(), g.idle.end(), part) != g.idle.end();
            };
            sp->setSpannerSegments(g.active);
            for (const auto& entry : g.properties) {
                entry.item->setOffsetChanged(false);
                entry.item->setProperty(entry.id, entry.value);
                entry.item->setPropertyFlags(entry.id, entry.flags);
            }
            for (auto* part : g.idle) sp->pushUnusedSegment(part);
            for (auto* part : inventory) {
                if (tracked(part)) continue;
                // This segment did not exist at the boundary being restored.
                // Keep the native allocation for redo/reuse, but never keep
                // the crossed system, user offsets or auto-place overrides.
                part->setOffsetChanged(false);
                part->setProperty(Property::OFFSET2, Value::fromValue(muse::PointF()));
                for (const auto id : geometryProperties) {
                    if (id == Property::OFFSET2 || id == Property::GENERATED) continue;
                    const auto v = part->propertyDefault(id);
                    if (v.isValid()) part->setProperty(id, v);
                    if (part->propertyFlags(id) == Flags::UNSTYLED) part->setPropertyFlags(id, Flags::STYLED);
                }
                sp->pushUnusedSegment(part);
            }
            sp->score()->setLayout(m_edit->first, m_edit->last, sp->staffIdx(), sp->staffIdx(), sp);
        }
        std::shared_ptr<Edit> m_edit;
        bool m_redoBoundary = false;
    };

    void captureOttavaGeometry(mu::engraving::EngravingItem* item)
    {
        if (item && item->type() == mu::engraving::ElementType::OTTAVA_SEGMENT) {
            m_pointer.ottava = std::make_unique<RestoreOttavaGeometry>(mu::engraving::toSpannerSegment(item)->spanner());
        }
    }

    struct PointerSession {
        int id = 0;
        QString kind;
        PointerPosition first;
        PointerPosition last;
        Qt::KeyboardModifiers modifiers;
        QByteArray mime;
        bool started = false;
        std::unique_ptr<RestoreOttavaGeometry> ottava;
        RestoreOttavaGeometry* ottavaUndo = nullptr;
        bool fast = false;
        bool lastAccepted = false;
        std::unique_ptr<mu::engraving::Selection> priorSelection;
    };
    PointerSession m_pointer;
    std::unique_ptr<QMimeData> m_scoreClipboard;
    mu::engraving::staff_idx_t m_clipboardStaves = 0;
    mu::engraving::Fraction m_clipboardSpan;
    muse::modularity::ContextPtr m_ctx;
    mu::project::INotationProjectPtr m_project;
    mu::notation::INotationPtr m_notation;
    bool m_editable = false;
    int m_page = 0;
};

inline int run(const muse::modularity::ContextPtr& ctx, const QString& input, const QString& output)
{
    if (input.isEmpty() || output.isEmpty()) {
        return 85;
    }
    const QFileInfo result(output);
    const QDir evidence = result.absoluteDir();
    if (!result.isAbsolute() || result.exists() || result.suffix() != "mscz" || !evidence.exists()) {
        return 86;
    }
    QSaveFile proof(evidence.filePath("compiled-native-evidence.json"));
    if (!proof.open(QIODevice::WriteOnly)) {
        return 87;
    }
    QJsonObject outcome;
    int code = 0;
    try {
        const QByteArray sourceBytes = sha256(input);
        LiveScore score(ctx, input);
        QTransform viewport;
        viewport.translate(47.0, 26.0);
        viewport.scale(0.91, 0.91);
        score.setView(viewport);
        const auto document = score.document();
        const auto before = score.paint();
        check(score.fixtureAnchor()->pitch() == 72, "fixture initial C5 missing");
        const auto bbox = score.fixtureAnchor()->canvasBoundingRect();
        const auto workerView = score.workerViewport();
        const auto workerAnchor = workerView.map(QPointF(bbox.center().x(), bbox.center().y()));
        const auto click = viewport.map(QPointF(bbox.center().x(), bbox.center().y()));
        check(!score.selectAt(click, score.fixtureAnchor()), "read-only accepted mutation selection");
        score.setMode(true);
        check(score.document() == document && score.view() == viewport, "edit toggle reset the score or view");
        check(score.selectAt(click, score.fixtureAnchor()), "native hit test didn't hit the visible C5");
        score.changePitch();
        check(score.fixtureAnchor()->pitch() == 84, "native pitch command didn't make C6");
        score.clearSelection();
        const auto edited = score.paint();
        const auto changedBox = score.fixtureAnchor()->canvasBoundingRect();
        const auto editedAnchor = workerView.map(QPointF(changedBox.center().x(), changedBox.center().y()));
        check(edited != before, "native note movement did not repaint");
        score.undo();
        check(score.fixtureAnchor()->pitch() == 72, "native undo did not restore C5");
        score.clearSelection();
        const auto undo = score.paint();
        check(undo != edited, "native undo left the edited image unchanged");
        const auto restoredBox = score.fixtureAnchor()->canvasBoundingRect();
        check(std::abs(restoredBox.center().y() - bbox.center().y()) < 0.5, "native undo did not restore the notehead staff position");
        score.redo();
        check(score.fixtureAnchor()->pitch() == 84, "native redo did not restore C6");
        score.clearSelection();
        score.setMode(false);
        check(score.document() == document && score.view() == viewport, "read toggle reset score or view");
        const auto redrawn = score.paint();
        check(redrawn != undo, "native redo did not repaint the restored image");
        check(std::abs(score.fixtureAnchor()->canvasBoundingRect().center().y() - changedBox.center().y()) < 0.5,
              "native redo did not restore the notehead staff position");
        score.save(output);
        LiveScore reopened(ctx, output);
        check(reopened.fixtureAnchor()->pitch() == 84, "reopened MSCZ did not preserve C6");
        check(sha256(input) == sourceBytes, "source MIDI changed");
        check(before.save(evidence.filePath("compiled-before.png")), "cannot write initial paint evidence");
        check(edited.save(evidence.filePath("compiled-after.png")), "cannot write final paint evidence");
        outcome = {
            {"scope", "Source-linked headless native probe; not the Astra tab integration"},
            {"pitchSequence", QJsonArray{72, 84, 72, 84}},
            {"viewPreserved", true},
            {"sameNativeDocument", true},
            {"visibleNativeUndoRedo", true},
            {"reopenedPitch", 84},
            {"fixtureWorkerX", workerAnchor.x()}, {"fixtureWorkerY", workerAnchor.y()},
            {"fixtureWorkerEditedX", editedAnchor.x()}, {"fixtureWorkerEditedY", editedAnchor.y()},
            {"midiSha256", QString::fromLatin1(sourceBytes)},
        };
    } catch (const std::exception& err) {
        code = 88;
        outcome = {{"scope", "source-linked native probe"}, {"reason", QString::fromUtf8(err.what())}};
    }
    if (proof.write(QJsonDocument(outcome).toJson()) == -1 || !proof.commit()) {
        return 89;
    }
    return code;
}


// Line-delimited requests are accepted exclusively from our local Astra QProcess.
// All notation mutations, picking and painting run on the upstream Qt UI thread.
// One horizontal band of an opaque frame, encoded on its own thread.
inline void astraEncodePngBand(const QImage* image, int y, int h, int quality, QByteArray* out, char* ok)
{
    const QImage view(image->constScanLine(y), image->width(), h, image->bytesPerLine(), image->format());
    out->clear();
    QBuffer buffer(out);
    *ok = buffer.open(QIODevice::WriteOnly) && view.save(&buffer, "PNG", quality);
}

inline void publish(const QJsonObject& packet)
{
    const auto bytes = QJsonDocument(packet).toJson(QJsonDocument::Compact);
    std::fputs("ASTRA_SCORE ", stdout);
    std::fwrite(bytes.constData(), 1, size_t(bytes.size()), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

inline int serve(const muse::modularity::ContextPtr& ctx, const QString& input, const QString& destination)
{
    try {
        const QByteArray operation = qgetenv("ASTRA_NATIVE_SCORE_OPERATION");
        const QFileInfo out(destination);
        if (!operation.isEmpty()) {
            // These read-only derivations use the same import, engraving, and
            // printing code as the in-place document. No editable state or MSCZ
            // is ever published from a range or automatic export worker.
            const bool printing = operation == "print";
            check(printing || operation == "preview", "unsupported native score worker operation");
            check(out.isAbsolute() && out.suffix() == (printing ? "pdf" : "mscz")
                  && !out.exists() && !out.isSymLink(), "invalid native projection destination");
            const auto sourceDigest = sha256(input);
            LiveScore projection(ctx, input);
            QJsonObject result = {
                {"request", 0}, {"renderer", "musescore-studio-4.6.5-native"},
                {"source_sha256", QString::fromLatin1(sourceDigest)}, {"pages", projection.pageCount()}
            };
            check(projection.pageCount() > 0, "native projection has no pages");
            if (printing) {
                check(out.absoluteDir().exists(), "native PDF directory missing");
                projection.exportPdf(destination);
            } else {
                bool validPage = false;
                const int page = qgetenv("ASTRA_NATIVE_SCORE_PAGE").toInt(&validPage);
                check(validPage, "invalid projection page");
                projection.setPage(page);
                const QByteArray requestedScale = qgetenv("ASTRA_NATIVE_SCORE_RASTER_SCALE");
                bool validScale = requestedScale.isEmpty();
                const double scale = requestedScale.isEmpty() ? 1.0 : requestedScale.toDouble(&validScale);
                check(validScale && (scale == 1.0 || scale == 1.5 || scale == 2.0),
                      "unsupported projection raster scale");
                const auto image = projection.paint(scale);
                QByteArray png;
                const qsizetype maxPngBytes = scale == 1.0 ? 749997 : 1499997;
                const bool encoded = [&] {
                    QBuffer buffer(&png);
                    return buffer.open(QIODevice::WriteOnly) && image.save(&buffer, "PNG", 88);
                }();
                if (!encoded || png.size() > maxPngBytes) {
                    png.clear();
                    QBuffer buffer(&png);
                    check(buffer.open(QIODevice::WriteOnly) && image.save(&buffer, "PNG"), "native projection paint failed");
                }
                check(!png.isEmpty() && png.size() <= maxPngBytes, "native projection exceeds raster frame budget");
                result.insert("page", page);
                result.insert("rasterScale", scale);
                result.insert("logicalHeight", qRound(image.height() / scale));
                result.insert("frame", QString::fromLatin1(png.toBase64()));
                result.insert("width", image.width());
                result.insert("height", image.height());
            }
            check(sha256(input) == sourceDigest, "native projection source changed");
            publish(result);
            return 0;
        }
        check(out.isAbsolute() && out.suffix() == "mscz", "invalid local native score destination");
        const auto folder = out.absoluteDir();
        const auto ensureOutput = [&]() {
            check(!QFileInfo(folder.absolutePath()).isSymLink(), "invalid local native folder");
            check(folder.exists() || QDir().mkpath(folder.absolutePath()), "cannot create native score folder");
        };
        auto score = std::make_unique<LiveScore>(ctx, out.exists() ? destination : input);
        score->setPage(0);
        int revision = 1;
        int saved = out.exists() ? 1 : 0;
        SavedScoreIdentity contentIdentity(score->undoIndex(), saved != 0);
        astra_native_panel::PaletteBridge palette;
        astra_rich_text::Session textSession;
#ifdef ASTRA_PANEL_EXPERIMENTAL_INSPECTOR
        astra_native_panel::PropertyBridge properties;
#endif
        QImage previousFrame;
        const double initialRasterScale = qgetenv("ASTRA_SCORE_INITIAL_RASTER_SCALE").toDouble();
        // The page is drawn at the screen's exact density (device pixel ratio x
        // zoom), as MuseScore's paint view draws into a backing store at the
        // device pixel ratio; not at fixed 1/1.5/2 steps the browser rescales.
        double rasterScale = std::isfinite(initialRasterScale) && initialRasterScale >= 0.5 && initialRasterScale <= 3.0
                             ? initialRasterScale : 1.0;
        int previousPage = -1;
        // Separate from damage mode: a complete raster remains valid even
        // when retained pixel-by-pixel differencing is not in use.
        int renderedPage = -1;
        int renderedRevision = -1;
        int frameSequence = 0;
        const QByteArray damageMode = qgetenv("ASTRA_SCORE_DAMAGE");
        const bool frameBands = qEnvironmentVariableIntValue("ASTRA_SCORE_FRAME_BANDS") == 1;
        // QA only: also paint the full page and count rows a region repaint got wrong.
        const bool verifyClip = qEnvironmentVariableIntValue("ASTRA_SCORE_VERIFY_CLIP") == 1;
        const bool damageEnabled = damageMode == "1" || damageMode == "2";
        QString contextMenu;
        const bool motionTrace = qEnvironmentVariableIntValue("ASTRA_SCORE_POINTER_TRACE") == 1;
        // Stage timing for latency QA only (ASTRA_SCORE_TIMING=1); off in normal use.
        const bool stageTiming = qEnvironmentVariableIntValue("ASTRA_SCORE_TIMING") == 1;
        QElapsedTimer stageClock;
        QJsonObject stage;
        const auto mark = [&](const char* name, qint64& since) {
            if (!stageTiming) return;
            const qint64 now = stageClock.nsecsElapsed();
            stage.insert(QString::fromLatin1(name), double(now - since) / 1000.0);
            since = now;
        };
        qint64 stageSince = 0;
        QElapsedTimer motionClock;
        mu::notation::AstraMotionMetrics motionMetrics;
        struct MotionScope {
            explicit MotionScope(mu::notation::AstraMotionMetrics* metrics) { mu::notation::setAstraMotionMetrics(metrics); }
            ~MotionScope() { mu::notation::setAstraMotionMetrics(nullptr); }
        };
        auto respond = [&](int request, bool withFrame, const QString& reason = {}, const QString& printed = {},
                           const QJsonObject& extra = {}) {
            const bool lightPointer = extra.value("pointerLight").toBool();
            if (stageTiming && !stageClock.isValid()) stageClock.start();
            QJsonObject response;
            if (lightPointer) {
                response = {{"request", request}, {"revision", revision},
                            {"page", score->pageIndex()+1}, {"selectedPitch", score->selectedPitch()}};
            } else {
                response = score->toolState();
                const QJsonObject stamps = {
                    {"request", request}, {"revision", revision}, {"saved", saved},
                    {"dirty", contentIdentity.dirty(score->undoIndex())},
                    {"editing", score->editable()}, {"undo", score->canUndo()}, {"redo", score->canRedo()},
                    {"page", score->pageIndex()+1}, {"pages", score->pageCount()},
                    {"systemLayout", score->systemLayoutState()}, {"timeDeleteKind",score->timeDeleteKind()}, {"transpose", score->transposeState()}, {"measureContext", score->measureState(!contextMenu.isEmpty())}, {"clipboardReady", score->clipboardReady()}, {"selectedPitch", score->selectedPitch()}, {"selectionKind", score->selectionKind()}
                };
                for (auto i = stamps.constBegin(); i != stamps.constEnd(); ++i) {
                    response.insert(i.key(), i.value());
                }
                if (!contextMenu.isEmpty()) {
                    response.insert("contextMenu", contextMenu);
                    contextMenu.clear();
                }
                const auto interaction = score->document()->interaction();
                response.insert("playbackSelection", score->playbackSelection());
                response.insert("textEditing", interaction->isTextEditingStarted());
                response.insert("textInfo", astra_rich_text::editingInfo(interaction));
                if (const auto* text = interaction->isTextEditingStarted() ? interaction->editedText() : nullptr;
                    text && text->cursor()) {
                    const auto rect = text->cursor()->cursorRect().translated(text->canvasPos());
                    const auto p = score->view().map(rect.topLeft().toQPointF());
                    response.insert("textCaret", QJsonArray{p.x(), p.y()});
                    response.insert("textSelection", text->cursor()->selectedText(false).toQString());
                }
                if (score->editable()) response.insert("filters", score->selectionFilterState());
                auto* textTarget = interaction->selection()->element();
                response.insert("textEditable", textTarget && interaction->textEditingAllowed(textTarget));
            }
            for (auto i = extra.constBegin(); i != extra.constEnd(); ++i) {
                response.insert(i.key(), i.value());
            }
            if (!reason.isEmpty()) {
                response.insert("error", reason);
            }
            if (!printed.isEmpty()) {
                response.insert("printedFile", printed);
            }
            mark("stateUs", stageSince);
            const bool trace = motionTrace && extra.contains("pointerKind");
            const auto finishTrace = [&](bool painted) {
                if (trace) {
                    response.insert("pointerWorkerUs", double(motionClock.nsecsElapsed())/1000.0);
                    response.insert("pointerPainted", painted);
                    response.insert("pointerPreviewMeasures", motionMetrics.previewMeasures);
                    response.insert("pointerOutlineSegments", motionMetrics.outlineSegments);
                }
            };
            if (withFrame) {
                renderedPage = renderedRevision = -1;
                QByteArray png;
                QBuffer buffer(&png);
                const QRectF pointerDamage = score->takePointerDamage();
                const bool region = !pointerDamage.isEmpty() && damageEnabled && request >= 0 &&
                                    previousPage == score->pageIndex() && !previousFrame.isNull() &&
                                    qFuzzyCompare(previousFrame.devicePixelRatio(), rasterScale);
                QImage image = region ? score->paintRegion(rasterScale, previousFrame, pointerDamage) : score->paint(rasterScale);
                if (region && image.size() != previousFrame.size()) image = score->paint(rasterScale);
                mark("paintUs", stageSince);
                if (stageTiming && region) stage.insert("regionPaint", true);
                if (region && verifyClip) {
                    const auto full = score->paint(rasterScale);
                    int rows = 0, first = -1, last = -1, x0 = 1 << 30, x1 = -1;
                    int maxDelta = 0, bigPixels = 0;
                    for (int y = 0; y < full.height(); ++y) {
                        const auto* f = reinterpret_cast<const QRgb*>(full.constScanLine(y));
                        const auto* g = reinterpret_cast<const QRgb*>(image.constScanLine(y));
                        if (std::memcmp(f, g, size_t(full.width())*4) == 0) continue;
                        ++rows; if (first < 0) first = y; last = y;
                        for (int x = 0; x < full.width(); ++x) if (f[x] != g[x]) {
                            x0 = qMin(x0, x); x1 = qMax(x1, x);
                            const int d = qMax(qMax(qAbs(qRed(f[x])-qRed(g[x])), qAbs(qGreen(f[x])-qGreen(g[x]))), qAbs(qBlue(f[x])-qBlue(g[x])));
                            maxDelta = qMax(maxDelta, d); if (d > 8) ++bigPixels;
                        }
                    }
                    stage.insert("clipMismatchRows", rows);
                    stage.insert("clipMaxDelta", maxDelta); stage.insert("clipBigPixels", bigPixels);
                    static int dumps = 0;
                    const QByteArray dumpDir = qgetenv("ASTRA_SCORE_VERIFY_DUMP");
                    if (rows > 10 && !dumpDir.isEmpty() && dumps < 3) {
                        full.save(QString::fromLocal8Bit(dumpDir) + QString("/m%1-full.png").arg(dumps));
                        image.save(QString::fromLocal8Bit(dumpDir) + QString("/m%1-region.png").arg(dumps));
                        previousFrame.save(QString::fromLocal8Bit(dumpDir) + QString("/m%1-prev.png").arg(dumps));
                        ++dumps;
                    }
                    if (rows) {
                        const QRectF lg = score->view().mapRect(pointerDamage);
                        stage.insert("clipMismatchAt", QJsonArray{first, last, x0, x1,
                            qRound(lg.top()*rasterScale), qRound(lg.bottom()*rasterScale), qRound(lg.left()*rasterScale), qRound(lg.right()*rasterScale)});
                    }
                    if (bigPixels) image = full;
                }
                QRect rect(0,0,image.width(),image.height());
                const int page = score->pageIndex();
                bool patch = false;
                QImage atlas;
                QJsonArray patchParts;
                if (damageEnabled && request >= 0 && previousPage == page && previousFrame.size() == image.size()) {
                    int x0 = image.width(), x1 = -1, y0 = image.height(), y1 = -1;
                    const int stripHeight = qRound(64*rasterScale);
                    int lefts[29], rights[29], tops[29], bottoms[29];
                    std::fill_n(lefts,29,image.width());std::fill_n(rights,29,-1);
                    std::fill_n(tops,29,image.height());std::fill_n(bottoms,29,-1);
                    for (int y = 0; y < image.height(); ++y) {
                        const auto* current = reinterpret_cast<const QRgb*>(image.constScanLine(y));
                        const auto* prior = reinterpret_cast<const QRgb*>(previousFrame.constScanLine(y));
                        if (std::memcmp(current, prior, size_t(image.width()) * sizeof(QRgb)) == 0) { continue; }
                        y0 = qMin(y0,y); y1 = y;
                        int a=0, z=image.width()-1;
                        while (current[a] == prior[a]) { ++a; }
                        while (current[z] == prior[z]) { --z; }
                        x0=qMin(x0,a);x1=qMax(x1,z);
                        const int i=y/stripHeight;
                        lefts[i]=qMin(lefts[i],a); rights[i]=qMax(rights[i],z);
                        tops[i]=qMin(tops[i],y); bottoms[i]=y;
                    }
                    if (x1 < x0) {
                        mark("diffUs", stageSince);
                        if (stageTiming) { stage.insert("frameKind", "same"); response.insert("nativeTiming", stage); }
                        renderedPage = page;
                        renderedRevision = revision;
                        response.insert("frameSeq", frameSequence);
                        finishTrace(true);
                        publish(response);
                        return;
                    }
                    const QRect damage(x0,y0,x1-x0+1,y1-y0+1);
                    if (stageTiming) stage.insert("damage", QJsonArray{x0,y0,x1-x0+1,y1-y0+1});
                    if (stageTiming) {
                        const int T = 32, tw = (image.width()+T-1)/T, th = (image.height()+T-1)/T;
                        int changed = 0;
                        for (int ty = 0; ty < th; ++ty) for (int tx = 0; tx < tw; ++tx) {
                            bool hit = false;
                            for (int y = ty*T; y < qMin(image.height(), ty*T+T) && !hit; ++y) {
                                const int w = qMin(T, image.width()-tx*T);
                                hit = std::memcmp(image.constScanLine(y)+tx*T*4, previousFrame.constScanLine(y)+tx*T*4, size_t(w)*4) != 0;
                            }
                            changed += hit;
                        }
                        stage.insert("tilesChanged", changed); stage.insert("tilesTotal", tw*th);
                    }
                    if (damage.width()*damage.height() < image.width()*image.height()/3) {
                        rect=damage;patch=true;
                    }
                    if (damageMode == "2") {
                        int aw=0, ah=0;
                        for (int i=0;i<29;++i) {
                            if (rights[i] < lefts[i]) { continue; }
                            aw=qMax(aw,rights[i]-lefts[i]+1); ah+=bottoms[i]-tops[i]+1;
                        }
                        if (aw*ah < (patch ? damage.width()*damage.height()/2 : image.width()*image.height()*4/5)) {
                            atlas=QImage(aw,ah,QImage::Format_ARGB32_Premultiplied);
                            atlas.fill(Qt::white);
                            int sy=0;
                            for (int i=0;i<29;++i) {
                                if (rights[i] < lefts[i]) { continue; }
                                const int w=rights[i]-lefts[i]+1, h=bottoms[i]-tops[i]+1;
                                patchParts.append(QJsonArray{lefts[i],tops[i],w,h,sy});
                                for (int y=0;y<h;++y) {
                                    std::memcpy(atlas.scanLine(sy+y),image.constScanLine(tops[i]+y)+lefts[i]*4,size_t(w)*4);
                                }
                                sy+=h;
                            }
                        }
                    }
                }
                mark("diffUs", stageSince);
                if (stageTiming && !stage.contains("damage"))
                    stage.insert("fullWhy", !damageEnabled ? "off" : request < 0 ? "request" : previousPage != page ? "page"
                                 : previousFrame.size() != image.size() ? "size" : "?");
                const auto source = !atlas.isNull() ? atlas : (patch ? image.copy(rect) : image);
                // The canvas is opaque: RGB is lossless and a quarter less to deflate.
                const QImage rendered = source.convertToFormat(QImage::Format_RGB888);
                // Qt PNG quality 88 requests DEFLATE level 1, not lossy quantization.
                constexpr int fastPngQuality = 88;
                // The canvas requires base64 strictly below 1,000,000 bytes.
                const qsizetype maxPngBytes = rasterScale == 1.0 ? 749997 : 1499997;
                // Large frames are encoded as horizontal bands in parallel when
                // the host can reassemble them (ASTRA_SCORE_FRAME_BANDS=1).
                const int cores = qBound(1, int(std::thread::hardware_concurrency()), 4);
                const int bands = frameBands && rendered.width()*rendered.height() >= 360000 && rendered.height() >= 64*cores ? cores : 1;
                std::vector<int> cuts(size_t(bands)+1);
                for (int b = 0; b <= bands; ++b) cuts[size_t(b)] = rendered.height()*b/bands;
                std::vector<QByteArray> bandPng(static_cast<size_t>(bands));
                std::vector<char> done(size_t(bands), 0);
                QByteArray* const outs = bandPng.data();
                char* const oks = done.data();
                const int* const rows = cuts.data();
                const QImage* const whole = &rendered;
                const int bandCount = bands;
                auto encodeAll = [outs, oks, rows, whole, bandCount, maxPngBytes](int quality) {
                    std::vector<std::thread> workers;
                    for (int b = 1; b < bandCount; ++b)
                        workers.emplace_back(astraEncodePngBand, whole, rows[b], rows[b+1]-rows[b], quality, outs+b, oks+b);
                    astraEncodePngBand(whole, rows[0], rows[1]-rows[0], quality, outs, oks);
                    for (auto& w : workers) w.join();
                    qsizetype total = 0;
                    for (int b = 0; b < bandCount; ++b) { if (!oks[b]) return false; total += outs[b].size(); }
                    return total <= maxPngBytes;
                };
                // Keep the compact PNG fallback when a frame exceeds its budget.
                bool encoded = encodeAll(fastPngQuality);
                if (!encoded) encoded = encodeAll(-1);
                check(encoded, "native paint failed or exceeded canvas frame budget");
                png = bandPng[0];
                mark("encodeUs", stageSince);
                response.insert("frame", QString::fromLatin1(png.toBase64()));
                if (bands > 1) {
                    QJsonArray parts;
                    for (int b = 0; b < bands; ++b) {
                        QJsonArray band{cuts[size_t(b)], cuts[size_t(b)+1]-cuts[size_t(b)]};
                        if (b) band.append(QString::fromLatin1(bandPng[size_t(b)].toBase64()));
                        parts.append(band);
                    }
                    response.insert("frameBands", parts);
                }
                mark("base64Us", stageSince);
                if (stageTiming) {
                    stage.insert("frameKind", !atlas.isNull() ? "strips" : patch ? "rect" : "full");
                    stage.insert("framePx", double(image.width()) * image.height());
                    stage.insert("sentPx", double(rendered.width()) * rendered.height());
                    qsizetype total = 0; for (const auto& b : bandPng) total += b.size();
                    stage.insert("pngBytes", double(total)); stage.insert("bands", bands);
                }
                if (damageEnabled) {
                    if (!atlas.isNull()) {
                        response.insert("patchBase", frameSequence);
                        response.insert("patchParts", patchParts);
                    } else if (patch) {
                        response.insert("patchBase", frameSequence);
                        response.insert("patchRect", QJsonArray{rect.x(),rect.y(),rect.width(),rect.height()});
                    }
                    response.insert("frameSeq", ++frameSequence);
                    previousFrame = image;
                    previousPage = page;
                }

                response.insert("width", image.width()); response.insert("rasterScale", rasterScale);
                response.insert("height", image.height()); response.insert("logicalHeight", qRound(image.height()/rasterScale));
                renderedPage = page;
                renderedRevision = revision;
            }
            finishTrace(withFrame);
            if (stageTiming && !response.contains("nativeTiming")) response.insert("nativeTiming", stage);
            publish(response);
        };
        respond(0, true);
        char line[4096];
        while (std::fgets(line, sizeof(line), stdin)) {
            const QByteArray bytes(line);
            if (!bytes.endsWith('\n')) {
                return 94;
            }
            const auto body = QJsonDocument::fromJson(bytes).object();
            const auto kind = body.value("kind").toString();
            if (kind == "quit") {
                return 0;
            }
            const int request = body.value("request").toInt(-1);
            if (stageTiming) {
                if (!stageClock.isValid()) stageClock.start();
                stage = QJsonObject{{"parseUs", 0.0}};
                stageSince = stageClock.nsecsElapsed();
            }
            if (kind == "shadow-note") {
                const double x=body.value("x").toDouble(-1), y=body.value("y").toDouble(-1);
                QJsonObject out{{"request",request},{"revision",revision},{"page",score->pageIndex()+1}};
                if (request > 2120000000 && request <= 2140000000 && body.value("baseRevision").toInt(-1)==revision
                    && body.value("page").toInt(-1)==score->pageIndex()+1 && std::isfinite(x) && std::isfinite(y)
                    && score->editable() && !score->pointerActive() && !score->document()->interaction()->isTextEditingStarted()) {
                    const auto overlay=score->shadowAt(QPointF(x,y), rasterScale);
                    for(auto i=overlay.begin();i!=overlay.end();++i) out.insert(i.key(),i.value());
                }
                publish(out);
                continue;
            }
            const auto beforeUndo = score->undoIndex();
            bool advanced = false;
            bool textMetadataChanged = false;
            // Revision is a fence for the live native document, not a prediction
            // based on the requested tool. Copy/select/failed no-op keep it;
            // any native undo transition or committed non-stack metadata advances it.
            const auto observeEdit = [&] {
                if (advanced) return false;
                const auto afterUndo = score->undoIndex();
                const bool changed = afterUndo != beforeUndo || textMetadataChanged;
                contentIdentity.observe(beforeUndo, afterUndo, kind == "undo" || kind == "redo", textMetadataChanged);
                if (changed) {
                    ++revision;
                    advanced = true;
                }
                return changed;
            };
            bool frame = false;
            bool reuseContextRaster = false;
            QString printed;
            QJsonObject extra;
            if (kind == "playbackCursorLayout") {
                try {
                    check(request>0 && body.value("baseRevision").toInt(-1)==revision, "score cursor revision changed");
                    check(body.value("page").toInt(-1)==score->pageIndex()+1, "score cursor page changed");
                    auto payload=score->playbackCursorLayout();
                    payload.insert("request",request);
                    payload.insert("revision",revision);
                    publish(payload);
                } catch (const std::exception& err) {
                    publish({{"request",request},{"revision",revision},{"error",QString::fromUtf8(err.what())}});
                }
                continue; // Dedicated read: never observe a mutation, reuse a raster, or change mode.
            }
            MotionScope metrics(motionTrace && kind == "pointer-gesture" ? &motionMetrics : nullptr);
            motionMetrics = {};
            try {
                check(request > 0 && body.value("baseRevision").toInt(-1) == revision, "谱面版本已变化，请重试");
                if (score->pointerActive()) {
                    check(kind == "pointer-gesture" || kind == "refreshFrame", "finish or cancel score drag first");
                }
                if (score->document()->interaction()->isTextEditingStarted()) {
                    check(kind == "text-action" || kind == "refreshFrame" || kind == "palette-catalog"
                          || kind == "palette-icon" || kind == "palette-icons", "请先完成文字编辑");
                }
                if (kind == "playbackSnapshot") {
                    // No image, page navigation, write, undo entry, or implicit save.
                    const auto bytes = score->playbackSnapshot();
                    QJsonObject payload{
                        {"request", request}, {"revision", revision}, {"saved", saved},
                        {"playbackMidi", QString::fromLatin1(bytes.toBase64())},
                        {"playbackBarMap", score->playbackBarMap()},
                    };
                    auto payloadSize=QJsonDocument(payload).toJson(QJsonDocument::Compact).size();
                    if (payloadSize >= 1024 * 1024) {
                        auto map = payload.value("playbackBarMap").toObject();
                        map.remove("clickGeometry"); // Optional when the bounded channel cannot carry it.
                        map.remove("beatGeometry");
                        payload.insert("playbackBarMap",map);
                        payloadSize=QJsonDocument(payload).toJson(QJsonDocument::Compact).size();
                    }
                    check(payloadSize < 1024 * 1024,
                          "native playback score exceeds channel budget");
                    publish(payload);
                    continue;
                }
                if (kind == "refreshFrame") {
                    if (body.contains("rasterScale")) {
                        const double next = body.value("rasterScale").toDouble(-1);
                        check(std::isfinite(next) && next >= 0.5 && next <= 3.0, "unsupported score raster scale");
                        rasterScale = next;
                    }
                    previousFrame = QImage(); previousPage=-1;
                    renderedPage = renderedRevision = -1; frame=true;
                } else if (kind == "mode") {
                    frame = true;
                    score->setMode(body.value("editing").toBool());
                } else if (kind == "select") {
                    check(score->editable(), "请先开启编辑模式");
                    const auto x = body.value("x").toDouble(-1);
                    const auto y = body.value("y").toDouble(-1);
                    check(std::isfinite(x) && std::isfinite(y) && x >= 0 && x < 960 && y >= 0 && y < 1800, "无效的谱面点击位置");
                    for (const auto* modifier : {"shift", "control"}) {
                        check(!body.contains(modifier) || body.value(modifier).isBool(), "invalid notation modifier");
                    }
                    score->click(QPointF(x,y), body.value("shift").toBool(), body.value("control").toBool(), true,
                                 body.value("cssScale").toDouble(0.0));
                    frame = true;
                } else if (kind == "selection-clear") {
                    check(score->editable() && !score->isInput(), "clear requires selection mode");
                    score->clearSelection();
                    frame = true;
                } else if (kind == "pan-hit") {
                    const auto x = body.value("x").toDouble(-1);
                    const auto y = body.value("y").toDouble(-1);
                    check(x >= 0 && x < 960 && y >= 0 && y < 1800, "invalid pan hit point");
                    extra.insert("panAllowed", score->canPanAt(QPointF(x,y), body.value("cssScale").toDouble(0.0)));
                } else if (kind == "tick-hit") {
                    const auto x = body.value("x").toDouble(-1);
                    const auto y = body.value("y").toDouble(-1);
                    check(std::isfinite(x) && std::isfinite(y) && x >= 0 && x < 960 && y >= 0 && y < 1800, "invalid tick hit point");
                    extra.insert("hitTick", score->tickAt(QPointF(x,y), body.value("cssScale").toDouble(0.0)));
                } else if (kind == "context-point") {
                    const auto x = body.value("x").toDouble(-1);
                    const auto y = body.value("y").toDouble(-1);
                    check(std::isfinite(x) && std::isfinite(y) && x >= 0 && x < 960 && y >= 0 && y < 1800,
                          "invalid context point");
                    contextMenu = score->contextClick(QPointF(x,y), reuseContextRaster, body.value("cssScale").toDouble(0.0));
                    frame = true;
                } else if (kind == "range-drag" || kind == "grip-drag") {
                    const auto point = [&](const char* xkey, const char* ykey) {
                        const auto x = body.value(xkey).toDouble(-1);
                        const auto y = body.value(ykey).toDouble(-1);
                        check(std::isfinite(x) && std::isfinite(y) && x >= 0 && x < 960 && y >= 0 && y < 1800,
                              "invalid native range point");
                        return QPointF(x, y);
                    };
                    const auto from = point("x0", "y0"), to = point("x1", "y1");
                    if (kind == "grip-drag") {
                        score->dragGrip(from, to);
                    } else {
                        score->lasso(from, to);
                    }
                    frame = true;
                } else if (kind == "pointer-gesture") {
                    if (motionTrace) motionClock.start();
                    extra = score->handlePointer(body);
                    const auto phase = body.value("phase").toString();
                    const auto pointerKind = extra.value("pointerKind").toString();
                    frame = pointerKind != "pan" && pointerKind != "blocked" &&
                            !extra.value("pointerNoPaint").toBool();
                    if (body.value("pointerV2").toBool() && (phase == "begin" || phase == "move")) {
                        extra.insert("pointerLight", true);
                    }
                } else if (kind == "line-key") {
                    score->editLineKey(body);
                    frame = true;
                } else if (kind == "select-more-query") {
                    extra.insert("selectMore", score->selectionMoreState());
                } else if (kind == "select-more-apply") {
                    score->matchSelectionMore(body);
                    frame = true;
                } else if (kind == "selection-filter") {
                    score->editSelectionFilter(body);
                    frame = true;
                } else if (kind == "navigate") {
                    score->navigate(body.value("mode").toString(), body.value("direction").toString());
                    frame = true;
                } else if (kind == "range-key") {
                    score->expandSelection(body.value("dir").toString(), body.value("unit").toString(), body.value("control").toBool());
                    frame = true;
                } else if (kind == "pitch") {
                    score->changePitch(body.value("up").toBool(), body.value("octave").toBool());
                    frame = true;
                } else if (kind == "undo" || kind == "redo") {
                    kind == "undo" ? score->undo() : score->redo();
                    frame = true;
                } else if (kind == "content") {
                    const auto action = body.value("action").toString();
                    const int count = body.contains("count") ? body.value("count").toInt(0) : 1;
                    if (action == "range-copy-at" || action == "drag-duplicate") {
                        score->duplicateAt(action, body);
                    } else if (action == "measure-number-mode" || action == "measure-exclude-number" ||
                               action == "measure-number-offset" || action == "measure-stretch") {
                        score->editMeasureProperty(action, body.value("value"));
                    } else if (action == "measure-properties-step") {
                        score->stepMeasureProperties(body.value("value").toObject(),body.value("direction").toString());
                        extra.insert("measurePropertiesStep",true);
                    } else if (action == "measure-properties") {
                        score->editMeasureProperties(body.value("value").toObject());
                    } else if (action == "measure-length" || action == "measure-length-reset") {
                        score->editMeasureLength(action, body);
                    } else {
                        score->editContent(action, count);
                    }
                    frame = action != "copy";
                } else if (kind == "transpose") {
                    score->transpose(body);
                    frame = true;
                } else if (kind == "tool") {
                    score->applyTool(body.value("tool").toString(), body.value("value").toString());
                    frame = true;
                } else if (kind == "palette-catalog") {
                    if (body.value("more").isString()) extra.insert("paletteMore", palette.more(body.value("more").toString()));
                    else extra.insert("palette", palette.catalog());
                } else if (kind == "palette-icon") {
                    extra.insert("paletteIcon", palette.icon(body.value("id").toString()));
                } else if (kind == "palette-icons") {
                    const auto ids = body.value("ids").toArray();
                    check(!ids.isEmpty() && ids.size() <= 16, "无效的原生符号图标批次");
                    QJsonObject icons;
                    for (const auto& id : ids) {
                        const auto cell = id.toObject();
                        const auto key = id.isString() ? id.toString() : cell.value("id").toString();
                        check(!key.isEmpty() && !icons.contains(key), "Invalid or duplicate native palette ID");
                        const double scale = cell.value("rasterScale").toDouble(1.0);
                        check(scale == 1.0 || scale == 1.5 || scale == 2.0, "invalid palette raster scale");
                        icons.insert(key, palette.icon(key, cell.value("width").toInt(), cell.value("height").toInt(), scale));
                    }
                    extra.insert("paletteIcons", icons);
                } else if (kind == "palette-apply" || kind == "palette-drop") {
                    check(score->editable(), "Edit mode required"); // MuseScore applies palette cells in note input too (to the note just entered)
                    const auto interaction = score->document()->interaction();
                    const auto mods = body.value("control").toBool() ? Qt::ControlModifier : Qt::NoModifier;
                    bool applied = false;
                    if (kind == "palette-apply") {
                        applied = palette.apply(interaction, body.value("id").toString(), mods);
                    } else {
                        const auto x = body.value("x").toDouble(-1);
                        const auto y = body.value("y").toDouble(-1);
                        check(body.value("page").toInt() == score->pageIndex() + 1
                              && std::isfinite(x) && std::isfinite(y)
                              && x >= 0 && x < 960 && y >= 0 && y < 1800,
                              "谱页或符号放置位置已变化");
                        const auto p = score->view().inverted().map(QPointF(x,y));
                        applied = palette.drop(interaction, body.value("id").toString(), muse::PointF(p.x(),p.y()), mods);
                    }
                    check(applied && score->undoIndex() != beforeUndo, "此位置不能应用该符号");
                    frame = true;
#ifdef ASTRA_PANEL_EXPERIMENTAL_INSPECTOR
                } else if (kind == "properties") {
                    check(score->editable(), "Edit mode required for score properties");
                    extra.insert("properties", properties.catalog(ctx, score->document(), score->project(), revision));
                } else if (kind == "property-set") {
                    check(score->editable(), "请先开启编辑模式");
                    properties.set(score->document()->interaction(), revision, body.value("id").toString(),
                                   body.value("value"), body.value("reset").toBool());
                    frame = score->undoIndex() != beforeUndo;
#endif
                } else if (kind == "text-add") {
                    check(score->editable(), "请先开启编辑模式");
                    if (textSession.add(score->document(), body.value("role").toString())) score->setPage(0);
                    frame = true;
                } else if (kind == "text-action") {
                    const auto op = body.value("operation").toString();
                    if (op == "start-at" || op == "caret-at" || op == "extend-to" || op == "select-word" || op == "select-text") {
                        const auto x = body.value("x").toDouble(-1), y = body.value("y").toDouble(-1);
                        check(score->editable() && x >= 0 && x < 960 && y >= 0 && y < 1800, "invalid native text caret point");
                        const auto q = score->view().inverted().map(QPointF(x,y));
                        textMetadataChanged = textSession.pointer(score->document(), score->project(), muse::PointF(q.x(),q.y()), op);
                        frame = true;
                    } else {
                        check(score->editable() && !score->isInput(), "请退出音符输入并进入编辑模式");
                        textMetadataChanged = textSession.dispatch(score->document(), score->project(), body);
                        frame = true;
                    }
                } else if (kind == "focus-measure") {
                    const double beat = body.value("beat").toDouble(-1.0);
                    check(std::isfinite(beat) && beat >= 0 && beat < 100000, "invalid score focus");
                    const auto tick = mu::engraving::Fraction::fromTicks(qRound(beat * mu::engraving::Constants::DIVISION));
                    auto* doc = score->score();
                    check(doc->lastMeasure() && tick < doc->lastMeasure()->endTick(), "score comparison outside right score");
                    auto* bar = doc->tick2measure(tick);
                    const auto* sys = bar ? bar->system() : nullptr;
                    const auto* target = sys ? sys->page() : nullptr;
                    const auto& pages = score->document()->elements()->pages();
                    const auto pos = std::find(pages.begin(), pages.end(), target);
                    check(pos != pages.end(), "score comparison position not in a printed page");
                    const auto rect = target->ldata()->bbox().translated(target->pos());
                    const double y = (sys->canvasPos().y() - rect.y()) * 920.0 / rect.width();
                    check(std::isfinite(y) && y >= 0 && y < 1800, "invalid score comparison system");
                    const int page = int(std::distance(pages.begin(), pos));
                    frame = score->pageIndex() != page;
                    score->setPage(page);
                    extra.insert("temporaryFocusY", y);
                } else if (kind == "view-config") {
                    // MuseScore View > Show: invisible elements, formatting, frames.
                    const auto interaction = score->document()->interaction();
                    auto config = interaction->scoreConfig();
                    if (body.value("showInvisible").isBool()) config.isShowInvisibleElements = body.value("showInvisible").toBool();
                    if (body.value("showUnprintable").isBool()) config.isShowUnprintableElements = body.value("showUnprintable").toBool();
                    if (body.value("showFrames").isBool()) config.isShowFrames = body.value("showFrames").toBool();
                    interaction->setScoreConfig(config);
                    score->score()->update();
                    extra.insert("viewConfig", QJsonObject{{"showInvisible", config.isShowInvisibleElements},
                        {"showUnprintable", config.isShowUnprintableElements}, {"showFrames", config.isShowFrames}});
                    frame = true;
                } else if (kind == "tidy-score") {
                    extra = score->tidyScore(body.value("title").toString().left(200), body.value("composer").toString().left(200));
                    frame = true;
                } else if (kind == "layout-tree") {
                    extra.insert("layoutTree", score->layoutTree());
                } else if (kind == "layout-set") {
                    score->layoutSet(body);
                    frame = true;
                } else if (kind == "vector-page") {
                    // Read only: the page's display list for the vector page view.
                    const int page = body.value("page").toInt(score->pageIndex() + 1);
                    check(page >= 1 && page <= score->pageCount(), "invalid score page");
                    const int current = score->pageIndex();
                    QJsonObject vector;
                    score->setPage(page - 1);
                    try { vector = score->vectorPage(); } catch (...) { score->setPage(current); throw; }
                    score->setPage(current);
                    extra.insert("vector", vector);
                    extra.insert("vectorPage", page);
                } else if (kind == "page-raster") {
                    // Read only: paint another page for the continuous page column
                    // without changing the page the person is editing.
                    const int page = body.value("page").toInt();
                    check(page >= 1 && page <= score->pageCount(), "invalid score page");
                    const double scale = body.value("rasterScale").toDouble(rasterScale);
                    check(std::isfinite(scale) && scale >= 0.5 && scale <= 3.0, "invalid page raster scale");
                    const int current = score->pageIndex();
                    QImage image;
                    score->setPage(page - 1);
                    try { image = score->paint(scale); } catch (...) { score->setPage(current); throw; }
                    score->setPage(current);
                    QByteArray png;
                    QBuffer out(&png);
                    check(out.open(QIODevice::WriteOnly) && image.convertToFormat(QImage::Format_RGB888).save(&out, "PNG", 88),
                          "page raster failed");
                    extra.insert("pageRaster", QString::fromLatin1(png.toBase64()));
                    extra.insert("pageRasterPage", page);
                    extra.insert("pageRasterScale", scale);
                    extra.insert("pageRasterHeight", qRound(image.height() / scale));
                } else if (kind == "followPage") {
                    const int page = body.value("page").toInt();
                    check(page >= 1 && page <= score->pageCount(), "invalid score playback page");
                    frame = score->pageIndex() != page - 1;
                    score->setPage(page - 1);
                } else if (kind == "find-score" || kind == "score-edge") {
                    using namespace mu::engraving;
                    auto doc = score->document();
                    const bool find = kind == "find-score";
                    const QString query = body.value("query").toString();
                    const QString edge = body.value("edge").toString();
                    if (find) {
                        check(!score->isInput() && !doc->interaction()->isTextEditingStarted() &&
                              body.value("query").isString() && query.size() <= 128,
                              "invalid native score find query");
                    } else {
                        check(edge == "start" || edge == "end", "invalid score edge");
                    }
                    auto* item = find ? doc->elements()->search(query.toStdString())
                                      : edge == "end" ? score->score()->lastMeasureMM() : nullptr;
                    if (find) {
                        if (item) doc->interaction()->select({item}, mu::notation::SelectType::SINGLE);
                        else doc->interaction()->clearSelection();
                        extra.insert("scoreFindResult", !item ? "none" : item->isPage() ? "page"
                                      : item->isMeasure() ? "measure" : "rehearsal");
                    }
                    if (item) {
                        const auto* m = item->isMeasureBase() ? toMeasureBase(item) : item->findMeasure();
                        const auto* sys = m ? m->system() : nullptr;
                        const auto* page = item->isPage() ? toPage(item) : sys ? sys->page() : nullptr;
                        const auto& pages = doc->elements()->pages();
                        const auto pos = std::find(pages.begin(),pages.end(),page);
                        if (pos != pages.end()) {
                            score->setPage(int(std::distance(pages.begin(),pos)));
                            if (item->isPage()) extra.insert("scoreRevealEdge","start");
                            else {
                                const auto rect = (find && m ? m->canvasBoundingRect() : item->canvasBoundingRect());
                                const auto paddedY = find ? rect.top() - score->score()->style().spatium() * 3.0
                                                          : rect.bottom() + score->score()->style().spatium() * 5.0;
                                const auto pt = score->view().map(QPointF(rect.x(),paddedY));
                                extra.insert("scoreRevealY",pt.y());
                                extra.insert("scoreRevealEdge",find ? "start" : "end");
                            }
                        }
                    } else if (!find && score->pageCount() > 0) {
                        score->setPage(0);
                        extra.insert("scoreRevealEdge","start");
                    }
                    frame = true;
                } else if (kind == "page") {
                    const int delta = body.value("delta").toInt();
                    check(delta == -1 || delta == 1, "invalid page movement");
                    score->setPage(score->pageIndex() + delta);
                    if (body.value("edge").toString() == "end") extra.insert("scoreRevealEdge","end");
                    frame = true;
                } else if (kind == "exportMusicXml") {
                    check(!score->isInput() && !score->document()->interaction()->isTextEditingStarted(),
                          "请先完成谱面文字或音符录入，再导出 MusicXML");
                    ensureOutput();
                    const auto name = mu::app::astra_musicxml_export::writeCurrent(ctx, score->document(), folder, revision);
                    extra.insert("musicXmlFile", name);
                    extra.insert("musicXmlRevision", revision);
                } else if (kind == "print") {
                    ensureOutput();
                    // A previous explicit export may still be open in a viewer.
                    // Never overwrite it, and do not report it as this revision.
                    const auto baseName = QString("piano-print-r%1").arg(revision);
                    QString pdf = folder.filePath("piano-print.pdf");
                    for (int attempt = 0; QFileInfo(pdf).exists(); ++attempt) {
                        check(attempt < 500 && !QFileInfo(pdf).isSymLink(), "invalid local PDF destination");
                        const auto suffix = attempt ? QString("-%1.pdf").arg(attempt) : QString(".pdf");
                        pdf = folder.filePath(baseName + suffix);
                    }
                    check(!QFileInfo(pdf).isSymLink(), "invalid local PDF destination");
                    score->exportPdf(pdf);
                    printed = QFileInfo(pdf).fileName();
                } else if (kind == "save") {
                    ensureOutput();
                    check(!QFileInfo(destination).isSymLink(), "invalid local score file");
                    score->save(destination);
                    observeEdit();
                    contentIdentity.markSaved(score->undoIndex());
                    saved = revision;
                } else if (kind == "reopen") {
                    check(!contentIdentity.dirty(score->undoIndex()), "请先保存当前谱面");
                    auto responseView = score->view();
                    auto loaded = std::make_unique<LiveScore>(ctx, destination);
                    loaded->setPage(qMin(score->pageIndex(), loaded->pageCount()-1));
                    loaded->setView(responseView);
                    loaded->setMode(score->editable());
                    score = std::move(loaded);
                    previousFrame = QImage();
                    previousPage = renderedPage = renderedRevision = -1;
                    contentIdentity.reset(score->undoIndex(), true);
                    saved = ++revision;
                    advanced = true;  // Reopen swaps the native document; do not compare their stacks.
                    frame = true;
                } else {
                    check(false, "unsupported notation action");
                }
                mark("commandUs", stageSince);
                QCoreApplication::processEvents();
                mark("eventsUs", stageSince);
                observeEdit();
                if (reuseContextRaster && renderedPage == score->pageIndex() && renderedRevision == revision) {
                    frame = false;
                }
                respond(request, frame, {}, printed, extra);
            } catch (const std::exception& err) {
                bool aborted = false;
                if (kind == "pointer-gesture") {
                    try { aborted = score->abortPointer(body); } catch (...) {}
                }
                const bool changed = observeEdit() || advanced || aborted;
                respond(request, changed, QString::fromUtf8(err.what()),
                        {}, aborted ? QJsonObject{{"pointerAborted", true}} : QJsonObject{});
            }
        }
    } catch (const std::exception& err) {
        publish({{"request", 0}, {"error", QString::fromUtf8(err.what())}});
        return 95;
    }
    return 0;
}

} // namespace mu::app::astra_native_probe
