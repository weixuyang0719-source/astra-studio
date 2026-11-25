#pragma once
// Windows VST3 view rectangles use physical client pixels, not Qt logical
// coordinates and not Win32 outer frame dimensions.
#include <windows.h>
#include <commctrl.h>
#include <limits>
#include "pluginterfaces/gui/iplugview.h"
#include "pluginterfaces/gui/iplugviewcontentscalesupport.h"

namespace astra::vst_host {
using namespace Steinberg;
struct PixelSize {
 int width=0,height=0;
 bool valid() const {return width>0&&height>0;}
 bool operator==(PixelSize b) const {return width==b.width&&height==b.height;}
};
inline PixelSize rect_size(const ViewRect& r) {
 const auto w=static_cast<int64>(r.right)-r.left,h=static_cast<int64>(r.bottom)-r.top;
 if(w<=0||h<=0||w>32767||h>32767)return {};
 return {static_cast<int>(w),static_cast<int>(h)};
}
inline UINT window_dpi(HWND w){const auto dpi=GetDpiForWindow(w);return dpi?dpi:96;}
inline RECT outer_rect(PixelSize size,DWORD style,DWORD exstyle,BOOL menu,UINT dpi){
 RECT r{0,0,size.width,size.height};
 if(size.valid()&&AdjustWindowRectExForDpi(&r,style,menu,exstyle,dpi))return r;
 return {};
}
inline RECT outer_rect(HWND w,PixelSize size,UINT dpi) {
 return outer_rect(size,static_cast<DWORD>(GetWindowLongPtrW(w,GWL_STYLE)),
                  static_cast<DWORD>(GetWindowLongPtrW(w,GWL_EXSTYLE)),GetMenu(w)!=nullptr,dpi);
}
inline PixelSize client_size(HWND w) {
 RECT r{};return GetClientRect(w,&r)?PixelSize{r.right-r.left,r.bottom-r.top}:PixelSize{};
}
struct EditorFrame final:IPlugFrame {
 HWND hwnd=nullptr;
 IPlugView* view=nullptr;
 bool busy=false,user_sizing=false,attached=false;
 UINT dpi=96;
 unsigned generation=0;
 PixelSize desired{};
 static constexpr UINT_PTR subclass_id=1;
 struct Busy {
  bool& b;
  explicit Busy(bool& v):b(v){b=true;}
  ~Busy(){b=false;}
 };
 tresult PLUGIN_API queryInterface(const TUID iid,void** p) override {
  if(!p)return kInvalidArgument;
  if(FUnknownPrivate::iidEqual(iid,IPlugFrame::iid)||FUnknownPrivate::iidEqual(iid,FUnknown::iid)){
   *p=static_cast<IPlugFrame*>(this);return kResultOk;
  }
  *p=nullptr;return kNoInterface;
 }
 uint32 PLUGIN_API addRef() override{return 1000;}
 uint32 PLUGIN_API release() override{return 1000;}

 bool install(HWND parent,IPlugView* editor) {
  hwnd=parent;view=editor;dpi=window_dpi(hwnd);desired={};
  if(SetWindowSubclass(hwnd,subclass,subclass_id,reinterpret_cast<DWORD_PTR>(this)))return true;
  reset();return false;
 }
 void reset() {
  if(hwnd)RemoveWindowSubclass(hwnd,subclass,subclass_id);
  hwnd=nullptr;view=nullptr;busy=false;attached=false;user_sizing=false;desired={};
 }
 void notify_scale() {
  FUnknownPtr<IPlugViewContentScaleSupport> scale(view);
  if(scale)scale->setContentScaleFactor(static_cast<float>(dpi)/96.f);
 }
 PixelSize reported_size() const {
  ViewRect r{};return view&&view->getSize(&r)==kResultOk?rect_size(r):PixelSize{};
 }
 bool fit_client(PixelSize s) const {
  if(!hwnd||!s.valid())return false;
  if(client_size(hwnd)==s)return true;
  const auto r=outer_rect(hwnd,s,dpi);
  if(r.right<=r.left||r.bottom<=r.top||
     !SetWindowPos(hwnd,nullptr,0,0,r.right-r.left,r.bottom-r.top,
                   SWP_NOMOVE|SWP_NOZORDER|SWP_NOACTIVATE))return false;
  return client_size(hwnd)==s;
 }
 bool fit_reported() {
  if(!desired.valid())desired=reported_size();
  if(!desired.valid())return false;
  Busy guard(busy);
  return fit_client(desired);
 }
 tresult PLUGIN_API resizeView(IPlugView* v,ViewRect* rect) override {
  if(!hwnd||v!=view||!rect)return kInvalidArgument;
  if(busy)return kResultFalse;
  const auto next=rect_size(*rect);
  if(!next.valid())return kInvalidArgument;
  const auto old=desired.valid()?desired:reported_size();
  Busy guard(busy);
  if(!fit_client(next))return kResultFalse;
  if(!(old==next)) {
   auto exact=*rect;
   const auto result=view->onSize(&exact);
   if(result!=kResultOk){if(old.valid())fit_client(old);return result;}
  }
  desired=next;++generation;return kResultOk;
 }
 void constrain(WPARAM edge,RECT& r) {
  if(busy||!attached||!view)return;
  const auto base=outer_rect(hwnd,{1,1},dpi);
  const int extraW=base.right-base.left-1,extraH=base.bottom-base.top-1;
  ViewRect requested{0,0,r.right-r.left-extraW,r.bottom-r.top-extraH};
  const bool allowed=view->canResize()==kResultOk;
  if(allowed&&rect_size(requested).valid()){
   if(view->checkSizeConstraint(&requested)!=kResultOk&&desired.valid())
    requested={0,0,desired.width,desired.height};
  }else if(desired.valid())requested={0,0,desired.width,desired.height};
  const auto s=rect_size(requested);
  if(!s.valid())return;
  const int w=s.width+extraW,h=s.height+extraH;
  if(edge==WMSZ_LEFT||edge==WMSZ_TOPLEFT||edge==WMSZ_BOTTOMLEFT)r.left=r.right-w;
  else r.right=r.left+w;
  if(edge==WMSZ_TOP||edge==WMSZ_TOPLEFT||edge==WMSZ_TOPRIGHT)r.top=r.bottom-h;
  else r.bottom=r.top+h;
 }
 void on_client_resize() {
  if(busy||!attached||!view||view->canResize()!=kResultOk)return;
  const auto next=client_size(hwnd);
  if(!next.valid()||desired==next)return;
  ViewRect r{0,0,next.width,next.height};
  Busy guard(busy);
  if(view->onSize(&r)==kResultOk)desired=next;
  else if(desired.valid())fit_client(desired);
 }
 static LRESULT CALLBACK subclass(HWND w,UINT msg,WPARAM wp,LPARAM lp,UINT_PTR id,DWORD_PTR ref) {
  auto* self=reinterpret_cast<EditorFrame*>(ref);
  switch(msg){
   case WM_NCDESTROY:
    RemoveWindowSubclass(w,subclass,id);self->hwnd=nullptr;break;
   case WM_ENTERSIZEMOVE:self->user_sizing=true;break;
   case WM_EXITSIZEMOVE:self->user_sizing=false;break;
   case WM_NCHITTEST:{
    const auto hit=DefSubclassProc(w,msg,wp,lp);
    if(self->attached&&self->view&&self->view->canResize()!=kResultOk&&hit>=HTLEFT&&hit<=HTBOTTOMRIGHT)
     return HTBORDER;
    return hit;
   }
   case WM_SIZING:
    if(lp&&self->attached){
     const auto result=DefSubclassProc(w,msg,wp,lp);
     self->constrain(wp,*reinterpret_cast<RECT*>(lp));return result?result:TRUE;
    }
    break;
   case WM_SIZE:
    if(self->user_sizing&&wp==SIZE_RESTORED)self->on_client_resize();
    break;
   case WM_DPICHANGED:
    if(self->attached&&!self->busy){
     const auto result=DefSubclassProc(w,msg,wp,lp);
     const auto newDpi=static_cast<UINT>(LOWORD(wp));
     if(newDpi&&newDpi!=self->dpi){
      self->dpi=newDpi;const auto previous=self->generation;
      self->notify_scale();
      if(self->generation==previous){
       const auto fresh=self->reported_size();
       if(fresh.valid())self->desired=fresh;
      }
      self->fit_reported();
     }
     return result;
    }
    break;
   default:break;
  }
  return DefSubclassProc(w,msg,wp,lp);
 }
};
} // namespace astra::vst_host
