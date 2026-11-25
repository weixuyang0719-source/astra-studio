#pragma once
#include <QtCore/Qt>
namespace mu::notation {
void setAstraPointerContext(Qt::KeyboardModifiers modifiers, bool cancelPending = false);
void clearAstraPointerContext();
int astraScoreViewport();
void setAstraScoreViewport(int page);
void setAstraRangePreview(bool enabled);
struct AstraMotionMetrics { int previewMeasures = 0; int outlineSegments = 0; };
AstraMotionMetrics* astraMotionMetrics();
void setAstraMotionMetrics(AstraMotionMetrics* metrics);
}
