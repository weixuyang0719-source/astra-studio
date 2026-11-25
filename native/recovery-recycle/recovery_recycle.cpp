#include <windows.h>
#include <shobjidl.h>
#include <shellapi.h>
// No DeleteFile, fallback, UI, recursive walk or permanent-delete mode.
extern "C" __declspec(dllexport) HRESULT __cdecl astra_recycle_file(const wchar_t* path) noexcept {
 if (!path || !*path) return E_INVALIDARG;
 HRESULT init=CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
 if (FAILED(init)) return init;
 IFileOperation* op=nullptr; IShellItem* item=nullptr;
 HRESULT hr=CoCreateInstance(CLSID_FileOperation,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&op));
 if (SUCCEEDED(hr)) hr=op->SetOperationFlags(FOFX_RECYCLEONDELETE|FOFX_EARLYFAILURE|FOF_NOERRORUI|FOF_SILENT|FOF_NOCONFIRMATION|FOF_NO_CONNECTED_ELEMENTS|FOF_NORECURSION);
 if (SUCCEEDED(hr)) hr=SHCreateItemFromParsingName(path,nullptr,IID_PPV_ARGS(&item));
 if (SUCCEEDED(hr)) hr=op->DeleteItem(item,nullptr);
 if (SUCCEEDED(hr)) hr=op->PerformOperations();
 if (SUCCEEDED(hr)) { BOOL aborted=TRUE; hr=op->GetAnyOperationsAborted(&aborted); if (SUCCEEDED(hr) && aborted) hr=HRESULT_FROM_WIN32(ERROR_CANCELLED); }
 if(item)item->Release(); if(op)op->Release(); CoUninitialize(); return hr;
}
