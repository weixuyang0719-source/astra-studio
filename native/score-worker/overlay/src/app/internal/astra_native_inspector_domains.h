// SPDX-License-Identifier: GPL-3.0-only
// Only explicit pinned MuseScore inspector controls reach PropertyItem.
// QML-derived control constraints are wire metadata, not custom score state.
#pragma once

#include <algorithm>
#include <cmath>
#include <optional>
#include <stdexcept>

#include <QColor>
#include <QJsonArray>
#include <QJsonObject>
#include <QRegularExpression>
#include <QVariant>

#include "engraving/dom/property.h"
#include "inspector/models/general/appearance/appearancesettingsmodel.h"
#include "inspector/models/notation/lines/glissandosettingsmodel.h"
#include "inspector/models/notation/lines/ottavasettingsmodel.h"
#include "inspector/models/notation/lines/pedalsettingsmodel.h"
#include "inspector/models/notation/lines/slurandtiesettingsmodel.h"
#include "inspector/models/notation/tempos/temposettingsmodel.h"
#include "inspector/models/notation/tuplets/tupletsettingsmodel.h"
#include "inspector/models/text/textsettingsmodel.h"
#include "inspector/models/notation/articulations/articulationsettingsmodel.h"
#include "inspector/models/notation/tremolos/tremolosettingsmodel.h"
#include "inspector/types/articulationtypes.h"
#include "inspector/types/tremolotypes.h"
#include "inspector/types/barlinetypes.h"
#include "inspector/types/texttypes.h"

namespace mu::app::astra_inspector_domains {
using namespace mu::engraving;
using namespace mu::inspector;

enum class Kind { Flag, Text, Color, Number, Point, Choice, RepeatList };
struct Option {
    int value;
    QString label;
};
struct Domain {
    Kind kind;
    double min = 0;
    double max = 0;
    double step = 1;
    bool integer = false;
    bool enabled = true;
    bool vertical = true;
    QString unit;
    QString label;
    QList<Option> choices;
};

inline std::optional<Domain> number(double min, double max, double step, const char* unit = "", bool integer = false)
{
    Domain value{Kind::Number};
    value.min = min; value.max = max; value.step = step; value.integer = integer;
    value.unit = QString::fromUtf8(unit);
    return value;
}

inline QString tr(const char* key) { return muse::qtrc("inspector", key); }

template<typename Enum>
inline Option option(Enum value, const char* label)
{
    return {static_cast<int>(value), tr(label)};
}

inline std::optional<Domain> choice(QList<Option> values)
{
    if (values.empty()) {
        return std::nullopt;
    }
    Domain result{Kind::Choice};
    result.choices = std::move(values);
    return result;
}

inline std::optional<Domain> fromStock(const QVariantList& options)
{
    QList<Option> projected;
    for (const auto& item : options) {
        const auto row = item.toMap();
        const auto title = row.value("text", row.value("title")).toString();
        bool ok = false;
        const int value = row.value("value").toInt(&ok);
        if (ok && !title.isEmpty() && std::none_of(projected.begin(), projected.end(),
                                                   [&](const auto& x) { return x.value == value; })) {
            projected.push_back({value, title});
        }
    }
    return choice(projected);
}

inline bool member(QByteArrayView name, std::initializer_list<QByteArrayView> names)
{
    return std::find(names.begin(), names.end(), name) != names.end();
}

// Only these real PropertyItem getters are inspected. No meta-method invocation,
// arbitrary QObject navigation, QML execution, or generic DOM Pid setting.
inline constexpr const char* FIELDS[] = {
    "isVisible", "isAutoPlaceAllowed", "isPlayable", "isSmall", "color", "offset",
    "leadingSpace", "measureWidth", "minimumDistance",
    "isStemless", "showStemSlash", "isHeadHidden", "isHeadSmall", "hasHeadParentheses",
    "headDirection", "dotPosition", "stemDirection", "directionType", "direction",
    "thickness", "length", "forceHorizontal", "featheringHeightLeft", "featheringHeightRight",
    "numberType", "bracketType", "lineThickness", "placement", "style", "position", "textPlacement",
    "tiePlacement", "lineStyle", "minLength", "startHookType", "endHookType", "lineType",
    "startHookHeight", "endHookHeight", "dashLineLength", "dashGapLength", "gapBetweenTextAndLine",
    "beginningText", "continuousText", "endText", "beginningTextOffset", "continuousTextOffset", "endTextOffset",
    "allowDiagonal", "isLineVisible", "height", "continuousHeight", "isNienteCircleVisible", "snapBefore", "snapAfter",
    "avoidBarLines", "centerOnNotehead", "dynamicSize", "showNumbersOnly", "ottavaType", "showText", "text",
    "followText", "tempo", "pauseDuration", "spacerHeight", "shouldStartWithLongInstrNames", "shouldResetBarNums",
    "firstSystemIndent", "showCourtesySignatures", "hasToShowCourtesy", "shouldShowCourtesy", "horizontalScale",
    "verticalScale", "type", "mode", "playCount", "repeatCount", "hasToPlayRepeats", "hasToShowTips",
    "isSpanToNextStaff", "spanFrom", "spanTo", "jumpTo", "playUntil", "continueAt", "label",
    "centerOnSymbol", "textLineSpacing", "symbolSize", "isSizeSpatiumDependent", "textType",
    "fontFamily", "fontSize", "frameType", "frameBorderColor", "frameFillColor", "frameThickness",
    "frameMargin", "frameCornerRadius", "verse", "avoidBarlines", "startOnUpperNote", "showCueNote"
};

inline bool visibleInStockControl(AbstractInspectorModel* model, QByteArrayView field, const PropertyItem* item)
{
    // PedalStyleSettings binds visibility to the rosette hook, not to
    // PropertyItem::isVisible (the native model hides its generic control).
    if (field == "isLineVisible" && item->propertyId() == Pid::LINE_VISIBLE) {
        if (const auto* pedal = qobject_cast<PedalSettingsModel*>(model)) {
            return pedal->isChangingLineVisibilityAllowed();
        }
    }
    return item->isVisible();
}

inline std::optional<Domain> forField(AbstractInspectorModel* model, QByteArrayView f, const PropertyItem* item)
{
    const auto pid = item->propertyId();
    if (pid == Pid::END) {
        // Pedal end ornament is a documented computed PropertyItem, not an
        // engraving Pid. MuseScore owns its coupled hooks/text/line mutation.
        if (f == "lineType") {
            if (const auto* pedal = qobject_cast<PedalSettingsModel*>(model)) {
                auto result = fromStock(pedal->possibleEndHookTypes());
                if (result) { result->label = tr("End hook"); }
                return result;
            }
        }
        return std::nullopt;
    }
    if (static_cast<int>(pid) < 0 || static_cast<int>(pid) >= static_cast<int>(Pid::END)) {
        return std::nullopt;
    }
    if (f == "placement" && pid == Pid::ARTICULATION_ANCHOR) {
        if (const auto* articulation = qobject_cast<ArticulationSettingsModel*>(model)) {
            using A = ArticulationTypes::Placement;
            auto result = choice({option(A::TYPE_AUTO,"Auto"),option(A::TYPE_TOP,"Above"),option(A::TYPE_BOTTOM,"Below")});
            result->label = tr("Placement");
            result->enabled = articulation->isPlacementAvailable();
            return result;
        }
        return std::nullopt;
    }
    if (f == "style") {
        if (pid == Pid::TREMOLO_STYLE && qobject_cast<TremoloSettingsModel*>(model) && !model->isEmpty()) {
            // TremoloSettingsModel includes only applicable, native two-chord
            // tremolos (half duration, not TAB). Do not duplicate that filter.
            using T = TremoloTypes::TremoloStyle;
            auto result = choice({option(T::STYLE_DEFAULT,"Default"),option(T::STYLE_TRADITIONAL,"Traditional"),
                                  option(T::STYLE_TRADITIONAL_ALTERNATE,"Traditional alternative")});
            result->label = tr("Style (between notes)");
            return result;
        }
        return std::nullopt;
    }
    if (const auto* tuplet = qobject_cast<TupletSettingsModel*>(model)) {
        if (f == "numberType") return fromStock(tuplet->possibleNumberTypes());
        if (f == "bracketType") return fromStock(tuplet->possibleBracketTypes());
    }
    if (const auto* slur = qobject_cast<SlurAndTieSettingsModel*>(model)) {
        if (f == "lineStyle" && slur->isLineStyleAvailable()) return fromStock(slur->possibleLineStyles());
        if ((f == "tiePlacement" && !slur->isTiePlacementAvailable()) || (f == "minLength" && !slur->isMinLengthAvailable())) {
            return std::nullopt;
        }
    }
    if (const auto* line = qobject_cast<TextLineSettingsModel*>(model)) {
        if (f == "startHookType") return fromStock(line->possibleStartHookTypes());
        if (f == "endHookType") return qobject_cast<PedalSettingsModel*>(model) ? std::nullopt : fromStock(line->possibleEndHookTypes());
    }
    if (const auto* gliss = qobject_cast<GlissandoSettingsModel*>(model); gliss && f == "lineType") {
        return fromStock(gliss->possibleLineTypes());
    }
    if (const auto* ottava = qobject_cast<OttavaSettingsModel*>(model); ottava && f == "ottavaType") {
        return fromStock(ottava->possibleOttavaTypes());
    }
    if (auto* text = qobject_cast<TextSettingsModel*>(model)) {
        if (f == "textType") return fromStock(text->textStyles());
        if (f == "textPlacement" && text->isSystemObjectBelowBottomStaff()) return std::nullopt;
        if (f == "symbolSize" && !text->isSymbolSizeAvailable()) return std::nullopt;
        if (f == "textLineSpacing" && !text->isLineSpacingAvailable()) return std::nullopt;
    }
    // These cases are only reached for fields bound in the pinned catalogue above.
    const auto type = propertyType(pid);
    switch (type) {
    case P_TYPE::BOOL: {
        Domain result{Kind::Flag};
        if (f == "isLineVisible" && pid == Pid::LINE_VISIBLE && qobject_cast<PedalSettingsModel*>(model)) {
            result.label = tr("Show line with rosette");
        }
        return result;
    }
    case P_TYPE::COLOR: return Domain{Kind::Color};
    case P_TYPE::POINT: {
        auto result = Domain{Kind::Point};
        result.min = -999.0; result.max = 999.0; result.step = 0.5;
        if (auto* appearance = qobject_cast<AppearanceSettingsModel*>(model); appearance && f == "offset") {
            result.vertical = appearance->isVerticalOffsetAvailable();
        }
        return result;
    }
    case P_TYPE::DIRECTION_V:
        return choice({option(DirectionV::AUTO,"Auto"),option(DirectionV::UP,"Up"),option(DirectionV::DOWN,"Down")});
    case P_TYPE::DIRECTION_H:
        return choice({option(DirectionH::AUTO,"Auto"),option(DirectionH::LEFT,"Left"),option(DirectionH::RIGHT,"Right")});
    case P_TYPE::PLACEMENT_V:
        return choice({option(PlacementV::ABOVE,"Above"),option(PlacementV::BELOW,"Below")});
    case P_TYPE::TIE_PLACEMENT:
        return choice({option(TiePlacement::AUTO,"Auto"),option(TiePlacement::INSIDE,"Inside"),option(TiePlacement::OUTSIDE,"Outside")});
    case P_TYPE::LINE_TYPE:
        return choice({option(LineType::SOLID,"Normal"),option(LineType::DASHED,"Dashed"),option(LineType::DOTTED,"Dotted")});
    case P_TYPE::KEY_MODE:
        return choice({option(KeyMode::UNKNOWN,"Unknown"),option(KeyMode::NONE,"None"),option(KeyMode::MAJOR,"Major"),
            option(KeyMode::MINOR,"Minor"),option(KeyMode::DORIAN,"Dorian"),option(KeyMode::PHRYGIAN,"Phrygian"),
            option(KeyMode::LYDIAN,"Lydian"),option(KeyMode::MIXOLYDIAN,"Mixolydian"),option(KeyMode::AEOLIAN,"Aeolian"),
            option(KeyMode::IONIAN,"Ionian"),option(KeyMode::LOCRIAN,"Locrian")});
    case P_TYPE::BARLINE_TYPE: {
        using B = mu::inspector::BarlineTypes::LineType;
        return choice({option(B::TYPE_NORMAL,"Single barline"),option(B::TYPE_DOUBLE,"Double barline"),
            option(B::TYPE_DASHED,"Dashed barline"),option(B::TYPE_FINAL,"Final barline"),option(B::TYPE_DOTTED,"Dotted barline"),
            option(B::TYPE_REVERSE_END,"Reverse final barline"),option(B::TYPE_HEAVY,"Heavy barline"),
            option(B::TYPE_DOUBLE_HEAVY,"Heavy double barline"),option(B::TYPE_START_REPEAT,"Left (start) repeat barline"),
            option(B::TYPE_END_REPEAT,"Right (end) repeat barline"),option(B::TYPE_END_START_REPEAT,"Right and left (end/start) repeat barline")});
    }
    case P_TYPE::STRING:
        if (member(f,{"beginningText","continuousText","endText","text","fontFamily","jumpTo","playUntil","continueAt","label"}))
            return Domain{Kind::Text};
        break;
    case P_TYPE::INT_VEC:
        if (f == "repeatCount") return Domain{Kind::RepeatList};
        break;
    default: break;
    }
    if (f == "frameType") {
        using F = mu::inspector::TextTypes::FrameType;
        return choice({option(F::FRAME_TYPE_NONE,"None"),option(F::FRAME_TYPE_SQUARE,"Rectangle"),option(F::FRAME_TYPE_CIRCLE,"Circle")});
    }
    // Pinned control domains: IncrementalPropertyControl inherited limits,
    // Inspector QML overrides, and TempoText::setTempo 5..999 BPM.
    if (f == "tempo" && pid == Pid::TEMPO) {
        auto result = number(5,999,0.5,"BPM");
        const auto* tempo = qobject_cast<TempoSettingsModel*>(model);
        result->enabled = tempo && !tempo->isFollowText()->isUndefined() && !tempo->isFollowText()->value().toBool();
        return result;
    }
    if (member(f,{"leadingSpace","measureWidth","minimumDistance"})) return number(-999,999,0.5);
    if (member(f,{"featheringHeightLeft","featheringHeightRight"})) return number(0,4,0.1);
    if (f == "length" && model->modelType() == AbstractInspectorModel::InspectorModelType::TYPE_STEM) return number(-10,10,0.5);
    if (f == "thickness") {
        if (model->modelType() == AbstractInspectorModel::InspectorModelType::TYPE_STEM) return number(0.01,4,0.01);
        if (qobject_cast<TextLineSettingsModel*>(model) || qobject_cast<GlissandoSettingsModel*>(model)) return number(0.1,10,0.01);
    }
    if (f == "lineThickness") return number(0.1,1,0.1);
    if (f == "minLength") return number(1,10,0.1);
    if (member(f,{"startHookHeight","endHookHeight"})) return number(-1000,1000,0.5);
    if (member(f,{"dashLineLength","dashGapLength"})) return number(0.1,100,0.1);
    if (f == "gapBetweenTextAndLine") return number(0,100,0.1);
    if (member(f,{"height","continuousHeight"})) return number(0.1,10,0.1);
    if (f == "dynamicSize") return number(0,1000,1,"%",true);
    if (f == "pauseDuration") return number(0,999,0.5,"s");
    if (f == "spacerHeight") return number(0,999,0.5);
    if (member(f,{"horizontalScale","verticalScale"})) return number(1,100,0.25,"×");
    if (f == "playCount") return number(2,999,1,"",true);
    if (f == "spanFrom") return number(-4,99,1,"",true);
    if (f == "spanTo") return number(-99,99,1,"",true);
    if (f == "textLineSpacing") return number(0,10,0.1);
    if (f == "symbolSize") return number(0,1000,1,"pt",true);
    if (f == "fontSize") return number(1,999,0.5,"pt");
    if (member(f,{"frameThickness","frameMargin"})) return number(0,5,0.1);
    if (f == "frameCornerRadius") return number(0,100,1,"",true);
    if (f == "verse") return number(1,999,1,"",true);
    return std::nullopt;
}

inline QJsonValue toWire(const Domain& domain, const QVariant& value)
{
    if (!value.isValid()) return {};
    if (domain.kind == Kind::Color) return value.value<QColor>().name(QColor::HexArgb);
    if (domain.kind == Kind::Point) {
        const auto p = value.toPointF();
        return QJsonObject{{"x",p.x()},{"y",p.y()}};
    }
    if (domain.kind == Kind::Choice) return value.toInt();
    return QJsonValue::fromVariant(value);
}

inline bool validNumber(const QJsonValue& value, const Domain& domain)
{
    const auto n = value.toDouble();
    return value.isDouble() && std::isfinite(n) && n >= domain.min && n <= domain.max
           && (!domain.integer || std::floor(n) == n);
}

inline std::optional<QVariant> fromWire(const Domain& domain, const QVariant& previous, const QJsonValue& wire)
{
    switch (domain.kind) {
    case Kind::Flag:
        if (wire.isBool()) return QVariant(wire.toBool());
        break;
    case Kind::Text: {
        const auto text = wire.toString();
        if (wire.isString() && text.size() <= 512 && !text.contains(QChar(u'\0'))) return QVariant(text);
        break;
    }
    case Kind::Color: {
        static const QRegularExpression hex(QStringLiteral("^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$"));
        const auto text = wire.toString();
        if (wire.isString() && hex.match(text).hasMatch() && QColor(text).isValid()) {
            return QVariant(QColor(text));
        }
        break;
    }
    case Kind::Number:
        if (validNumber(wire, domain)) {
            return domain.integer ? QVariant(wire.toInt()) : QVariant(wire.toDouble());
        }
        break;
    case Kind::Point: {
        const auto obj = wire.toObject();
        const auto x = obj.value("x"), y = obj.value("y");
        if (wire.isObject() && validNumber(x,domain) && (!domain.vertical || validNumber(y,domain))) {
            return QVariant(QPointF(x.toDouble(), domain.vertical ? y.toDouble() : previous.toPointF().y()));
        }
        break;
    }
    case Kind::Choice:
        if (wire.isDouble() && std::any_of(domain.choices.begin(),domain.choices.end(),[&](const auto& c) {
            return wire.toDouble() == c.value;
        })) {
            return QVariant(wire.toInt());
        }
        break;
    case Kind::RepeatList: {
        static const QRegularExpression repeats(QStringLiteral("^\\s*[1-9][0-9]{0,2}(?:\\s*,\\s*[1-9][0-9]{0,2}){0,98}\\s*$"));
        const auto text = wire.toString();
        if (wire.isString() && text.size() < 512 && repeats.match(text).hasMatch()) return QVariant(text);
        break;
    }
    }
    return std::nullopt;
}

inline QJsonObject descriptor(const Domain& domain, const PropertyItem* item)
{
    static const char* types[] = {"boolean","text","color","number","point","choice","text"};
    const bool mixed = item->isUndefined();
    QJsonObject r{{"type", types[static_cast<int>(domain.kind)]}, {"mixed",mixed},
                  {"enabled",item->isEnabled() && domain.enabled}, {"styled",item->isStyled()},
                  {"resettable",item->propertyId() != Pid::END && item->isModified()},
                  {"value",mixed ? QJsonValue(QJsonValue::Null) : toWire(domain,item->value())}};
    if (domain.kind == Kind::Number || domain.kind == Kind::Point) {
        r.insert("min",domain.min);r.insert("max",domain.max);r.insert("step",domain.step);
        r.insert("unit",domain.unit);r.insert("integer",domain.integer);r.insert("vertical",domain.vertical);
    }
    if (domain.kind == Kind::Choice) {
        QJsonArray opts;
        for (const auto& opt : domain.choices) opts.append(QJsonObject{{"value",opt.value},{"label",opt.label}});
        r.insert("choices",opts);
    }
    return r;
}
} // namespace mu::app::astra_inspector_domains
