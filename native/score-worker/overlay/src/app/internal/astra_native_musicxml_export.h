// SPDX-License-Identifier: GPL-3.0-only
// The pinned MuseScore writer serializes the current editing document itself.
// This is an explicit interchange copy, never an import seed or autosave.
#pragma once

#include <stdexcept>

#include <QDir>
#include <QFileInfo>
#include <QTemporaryFile>
#include <QXmlStreamReader>

#include "global/io/buffer.h"
#include "modularity/ioc.h"
#include "notation/inotation.h"
#include "project/inotationwritersregister.h"

namespace mu::app::astra_musicxml_export {

inline void require(bool ok, const char* message)
{
    if (!ok) {
        throw std::runtime_error(message);
    }
}

inline bool ownDirectory(const QDir& folder)
{
    QFileInfo info(folder.absolutePath());
    return folder.isAbsolute() && info.isDir() && !info.isSymLink() && !info.isJunction()
           && QDir::cleanPath(info.canonicalFilePath()) == QDir::cleanPath(folder.absolutePath());
}

inline QString writeCurrent(const muse::modularity::ContextPtr& ctx, const notation::INotationPtr& notation,
                            const QDir& folder, int revision)
{
    using namespace mu::project;
    require(notation && revision > 0 && ownDirectory(folder), "无法验证 MusicXML 导出目录");
    const auto interaction = notation->interaction();
    require(interaction && !interaction->isTextEditingStarted() && !interaction->noteInput()->isNoteInputMode(),
            "请先完成谱面文字或音符录入，再导出 MusicXML");
    const auto writers = muse::modularity::_ioc(ctx)->resolve<INotationWritersRegister>("astra-native-musicxml");
    require(bool(writers), "缺少原生 MusicXML 写入组件");
    const auto writer = writers->writer("musicxml");
    require(writer && writer->supportsUnitType(INotationWriter::UnitType::PER_PART), "缺少可用的原生 MusicXML 写入组件");
    const auto undo = notation->undoStack();
    const auto before = undo->currentStateIndex();
    const auto clean = undo->isStackClean();
    muse::io::Buffer data;
    require(data.open(muse::io::IODevice::WriteOnly), "无法创建 MusicXML 缓冲区");
    const INotationWriter::Options options = {
        { INotationWriter::OptionKey::UNIT_TYPE, muse::Val(INotationWriter::UnitType::PER_PART) }
    };
    require(bool(writer->write(notation, data, options)), "原生 MusicXML 导出失败");
    require(before == undo->currentStateIndex() && clean == undo->isStackClean(), "MusicXML 导出修改了原文档");
    const auto& buffer = data.data();
    require(buffer.size() > 32 && buffer.size() <= 64 * 1024 * 1024, "MusicXML 大小无效");
    const auto size = qint64(buffer.size());
    const auto body = QByteArray::fromRawData(buffer.constChar(), qsizetype(size));
    // QXmlStreamReader performs no network/entity lookup. This checks the
    // native root, not a hand-written transcription or parser/score model.
    QXmlStreamReader xml(body);
    require(xml.readNextStartElement() && xml.name() == QStringLiteral("score-partwise"), "MusicXML 输出无效");

    QTemporaryFile temp(folder.filePath(QStringLiteral(".piano-score-XXXXXX.tmp")));
    require(temp.open() && temp.write(buffer.constChar(), size) == size && temp.flush(), "无法暂存 MusicXML");
    temp.close();
    const auto stem = QStringLiteral("piano-score-r%1").arg(revision);
    for (int attempt = 1; attempt <= 500; ++attempt) {
        const auto suffix = attempt == 1 ? QString() : QStringLiteral("-%1").arg(attempt);
        const auto path = folder.filePath(stem + suffix + QStringLiteral(".musicxml"));
        const QFileInfo pathInfo(path);
        require(ownDirectory(folder) && !pathInfo.isSymLink() && !pathInfo.isJunction(), "MusicXML 导出目标不安全");
        if (pathInfo.exists()) {
            continue;
        }
        // Same directory; rename will not overwrite another export even if it
        // appeared between the existence check and publishing this result.
        if (temp.rename(path)) {
            temp.setAutoRemove(false);
            return QFileInfo(path).fileName();
        }
        require(QFileInfo(path).exists(), "无法发布 MusicXML");
    }
    throw std::runtime_error("已有过多 MusicXML 导出文件");
}
} // namespace mu::app::astra_musicxml_export
