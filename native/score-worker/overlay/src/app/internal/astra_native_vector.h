#pragma once
// Vector display list of one score page, recorded from the same QPainter
// calls the raster path makes (QPainterProvider -> QPainter -> paint engine).
// The page view in the Astra window replays it at any zoom in one frame,
// as MuseScore's paint view repaints its vector scene at the current matrix.
//
// Paths are written in canvas units (the 960-wide page) with the painter
// transform applied. Text and music glyphs become outlines of the same
// QFont QPainter would use; each distinct outline is written once.

#include <QBrush>
#include <QColor>
#include <QFont>
#include <QHash>
#include <QImage>
#include <QJsonArray>
#include <QJsonObject>
#include <QPaintDevice>
#include <QPaintEngine>
#include <QPainterPath>
#include <QPen>
#include <QTextItem>
#include <QTransform>
#include <cmath>

namespace astra_vector {

inline QString num(double v)
{
    const double r = std::round(v * 100.0) / 100.0;
    return QString::number(r == 0.0 ? 0.0 : r, 'g', 8);
}

inline QString pathData(const QPainterPath& path, const QTransform& t)
{
    QString d;
    d.reserve(path.elementCount() * 14);
    for (int i = 0; i < path.elementCount(); ++i) {
        const QPainterPath::Element e = path.elementAt(i);
        if (e.type == QPainterPath::CurveToElement && i + 2 < path.elementCount()) {
            const QPointF a = t.map(QPointF(e.x, e.y));
            const auto b0 = path.elementAt(i + 1), c0 = path.elementAt(i + 2);
            const QPointF b = t.map(QPointF(b0.x, b0.y)), c = t.map(QPointF(c0.x, c0.y));
            d += QLatin1Char('C') + num(a.x()) + QLatin1Char(' ') + num(a.y()) + QLatin1Char(' ') + num(b.x()) + QLatin1Char(' ')
                 + num(b.y()) + QLatin1Char(' ') + num(c.x()) + QLatin1Char(' ') + num(c.y());
            i += 2;
            continue;
        }
        const QPointF p = t.map(QPointF(e.x, e.y));
        d += QLatin1Char(e.type == QPainterPath::MoveToElement ? 'M' : 'L') + num(p.x()) + QLatin1Char(' ') + num(p.y());
    }
    return d;
}

inline QString colorOf(const QColor& c, double opacity)
{
    const double a = c.alphaF() * opacity;
    if (a >= 0.999) return c.name(QColor::HexRgb);
    return QStringLiteral("rgba(%1,%2,%3,%4)").arg(c.red()).arg(c.green()).arg(c.blue()).arg(num(a));
}

class Engine : public QPaintEngine
{
public:
    Engine() : QPaintEngine(QPaintEngine::AllFeatures) {}
    bool begin(QPaintDevice*) override { return true; }
    bool end() override { return true; }
    Type type() const override { return QPaintEngine::User; }

    void updateState(const QPaintEngineState& st) override
    {
        const auto f = st.state();
        if (f & DirtyPen) m_pen = st.pen();
        if (f & DirtyBrush) m_brush = st.brush();
        if (f & DirtyTransform) m_t = st.transform();
        if (f & DirtyOpacity) m_opacity = st.opacity();
        if (f & (DirtyClipPath | DirtyClipRegion | DirtyClipEnabled)) {
            // Only the page clip set before painting is kept (see clip()).
        }
    }

    void drawPath(const QPainterPath& path) override { add(path, true, true); }
    void drawPolygon(const QPointF* pts, int n, PolygonDrawMode mode) override
    {
        if (n < 2) return;
        QPainterPath path;
        path.moveTo(pts[0]);
        for (int i = 1; i < n; ++i) path.lineTo(pts[i]);
        if (mode != PolylineMode) path.closeSubpath();
        path.setFillRule(mode == WindingMode ? Qt::WindingFill : Qt::OddEvenFill);
        add(path, mode != PolylineMode, true);
    }
    void drawPixmap(const QRectF&, const QPixmap&, const QRectF&) override { ++m_skipped; }
    void drawImage(const QRectF&, const QImage&, const QRectF&, Qt::ImageConversionFlags) override { ++m_skipped; }

    // QPainter fills text with the pen colour.
    void drawTextItem(const QPointF& p, const QTextItem& ti) override
    {
        const QFont font = ti.font();
        const QString key = font.key() + QLatin1Char('|') + ti.text();
        int id = m_glyphIds.value(key, -1);
        if (id < 0) {
            QPainterPath outline;
            outline.addText(0, 0, font, ti.text());
            id = int(m_glyphs.size());
            m_glyphIds.insert(key, id);
            m_glyphs.append(pathData(outline, QTransform()));
        }
        const QTransform t = QTransform::fromTranslate(p.x(), p.y()) * m_t;
        QJsonArray op{QStringLiteral("g"), id, colorOf(m_pen.color(), m_opacity)};
        if (qFuzzyIsNull(t.m12()) && qFuzzyIsNull(t.m21()) && qFuzzyCompare(t.m11(), t.m22())) {
            op << std::round(t.m11() * 100000.0) / 100000.0 << num(t.dx()).toDouble() << num(t.dy()).toDouble();
        } else {
            op << t.m11() << t.m12() << t.m21() << t.m22() << num(t.dx()).toDouble() << num(t.dy()).toDouble();
        }
        m_ops.append(op);
    }

    QJsonObject result(const QSizeF& size, const QRectF& clip) const
    {
        QJsonArray glyphs;
        for (const auto& g : m_glyphs) glyphs.append(g);
        return QJsonObject{{"w", size.width()}, {"h", size.height()},
                           {"clip", QJsonArray{clip.x(), clip.y(), clip.width(), clip.height()}},
                           {"glyphs", glyphs}, {"ops", m_ops}, {"skipped", m_skipped}};
    }

private:
    void add(const QPainterPath& path, bool fill, bool stroke)
    {
        const double scale = std::sqrt(std::abs(m_t.determinant()));
        const bool filled = fill && m_brush.style() != Qt::NoBrush;
        const bool stroked = stroke && m_pen.style() != Qt::NoPen;
        if (!filled && !stroked) return;
        const QString d = pathData(path, m_t);
        if (filled) {
            m_ops.append(QJsonArray{path.fillRule() == Qt::WindingFill ? QStringLiteral("f") : QStringLiteral("F"), d,
                                    colorOf(m_brush.color(), m_opacity)});
        }
        if (stroked) {
            // Width 0 (and cosmetic pens) is one device pixel in QPainter.
            const double w = m_pen.isCosmetic() || m_pen.widthF() == 0 ? -std::max(1.0, m_pen.widthF())
                                                                         : m_pen.widthF() * scale;
            const int cap = m_pen.capStyle() == Qt::FlatCap ? 0 : m_pen.capStyle() == Qt::RoundCap ? 1 : 2;
            const int join = m_pen.joinStyle() == Qt::MiterJoin || m_pen.joinStyle() == Qt::SvgMiterJoin ? 0
                             : m_pen.joinStyle() == Qt::RoundJoin ? 1 : 2;
            QJsonArray op{QStringLiteral("s"), d, colorOf(m_pen.color(), m_opacity), num(w).toDouble(), cap, join};
            if (m_pen.style() != Qt::SolidLine) {
                QJsonArray dash;
                const double unit = m_pen.widthF() > 0 ? m_pen.widthF() * scale : 1.0;
                for (double v : m_pen.dashPattern()) dash.append(num(v * unit).toDouble());
                op.append(dash);
            }
            m_ops.append(op);
        }
    }

    QPen m_pen;
    QBrush m_brush;
    QTransform m_t;
    double m_opacity = 1.0;
    QJsonArray m_ops;
    QVector<QString> m_glyphs;
    QHash<QString, int> m_glyphIds;
    int m_skipped = 0;
};

// A paint device with the metrics of the raster page image (same logical DPI,
// so point-sized fonts get the same pixel size).
class Device : public QPaintDevice
{
public:
    explicit Device(const QSize& size) : m_size(size)
    {
        const QImage probe(1, 1, QImage::Format_ARGB32_Premultiplied);
        m_dpiX = probe.logicalDpiX();
        m_dpiY = probe.logicalDpiY();
    }
    QPaintEngine* paintEngine() const override { return &m_engine; }
    Engine& engine() const { return m_engine; }

protected:
    int metric(PaintDeviceMetric m) const override
    {
        switch (m) {
        case PdmWidth: return m_size.width();
        case PdmHeight: return m_size.height();
        case PdmWidthMM: return qRound(m_size.width() * 25.4 / m_dpiX);
        case PdmHeightMM: return qRound(m_size.height() * 25.4 / m_dpiY);
        case PdmNumColors: return INT_MAX;
        case PdmDepth: return 32;
        case PdmDpiX: case PdmPhysicalDpiX: return m_dpiX;
        case PdmDpiY: case PdmPhysicalDpiY: return m_dpiY;
        case PdmDevicePixelRatio: return 1;
        case PdmDevicePixelRatioScaled: return int(QPaintDevice::devicePixelRatioFScale());
        default: return 0;
        }
    }

private:
    QSize m_size;
    int m_dpiX = 96, m_dpiY = 96;
    mutable Engine m_engine;
};

} // namespace astra_vector
