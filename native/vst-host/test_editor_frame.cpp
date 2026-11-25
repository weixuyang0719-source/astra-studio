#include "editor_frame.hpp"
#include <cstdlib>
#include <cmath>
#include <iostream>
using namespace astra::vst_host;
#define REQUIRE(x) do{if(!(x)){std::cerr<<"failed at line "<<__LINE__<<": "<<#x<<"\n";std::exit(1);}}while(false)
struct FakeView final:IPlugView,IPlugViewContentScaleSupport {
 ViewRect r{10,20,650,380};
 IPlugFrame* frame=nullptr;
 HWND child=nullptr;
 bool resizable=true,reject=false,request_on_scale=false;
 float scale=0;
 int changes=0,checks=0;
 tresult PLUGIN_API queryInterface(const TUID id,void** p) override{
  if(FUnknownPrivate::iidEqual(id,IPlugViewContentScaleSupport::iid))*p=static_cast<IPlugViewContentScaleSupport*>(this);
  else if(FUnknownPrivate::iidEqual(id,IPlugView::iid)||FUnknownPrivate::iidEqual(id,FUnknown::iid))*p=static_cast<IPlugView*>(this);
  else{*p=nullptr;return kNoInterface;}
  return kResultOk;
 }
 uint32 PLUGIN_API addRef() override{return 1;}
 uint32 PLUGIN_API release() override{return 1;}
 tresult PLUGIN_API isPlatformTypeSupported(FIDString) override{return kResultOk;}
 tresult PLUGIN_API attached(void* p,FIDString) override{
  const auto s=rect_size(r);
  child=CreateWindowExW(0,L"STATIC",L"",WS_CHILD,0,0,s.width,s.height,static_cast<HWND>(p),nullptr,nullptr,nullptr);
  return child?kResultOk:kResultFalse;
 }
 tresult PLUGIN_API removed() override{if(child)DestroyWindow(child);return kResultOk;}
 tresult PLUGIN_API onWheel(float) override{return kResultFalse;}
 tresult PLUGIN_API onKeyDown(char16,int16,int16) override{return kResultFalse;}
 tresult PLUGIN_API onKeyUp(char16,int16,int16) override{return kResultFalse;}
 tresult PLUGIN_API getSize(ViewRect* out) override{*out=r;return kResultOk;}
 tresult PLUGIN_API onSize(ViewRect* next) override{
  ++changes;
  if(reject)return kResultFalse;
  r=*next;
  if(child){const auto s=rect_size(r);SetWindowPos(child,nullptr,0,0,s.width,s.height,SWP_NOZORDER|SWP_NOACTIVATE);}
  return kResultOk;
 }
 tresult PLUGIN_API onFocus(TBool) override{return kResultOk;}
 tresult PLUGIN_API setFrame(IPlugFrame* f) override{frame=f;return kResultOk;}
 tresult PLUGIN_API canResize() override{return resizable?kResultOk:kResultFalse;}
 tresult PLUGIN_API checkSizeConstraint(ViewRect* next) override{
  ++checks;next->right=next->left+std::max(600,next->right-next->left);
  next->bottom=next->top+std::max(340,next->bottom-next->top);return kResultOk;
 }
 tresult PLUGIN_API setContentScaleFactor(float s) override{
  scale=s;
  if(request_on_scale){ViewRect size{0,0,int(std::lround(620*s)),int(std::lround(350*s))};return frame->resizeView(this,&size);}
  return kResultOk;
 }
};
int main(){
 SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
 constexpr auto style=WS_OVERLAPPEDWINDOW;
 const auto m96=outer_rect({640,360},style,0,FALSE,96);
 const auto m144=outer_rect({640,360},style,0,FALSE,144);
 const auto m192=outer_rect({640,360},style,0,FALSE,192);
 REQUIRE(m96.right-m96.left>640&&m96.bottom-m96.top>360);
 REQUIRE(m144.bottom-m144.top>m96.bottom-m96.top);
 REQUIRE(m192.bottom-m192.top>m144.bottom-m144.top);
 auto w=CreateWindowExW(0,L"STATIC",L"hidden fixture",style,0,0,700,500,nullptr,nullptr,nullptr,nullptr);
 REQUIRE(w&&!IsWindowVisible(w));
 EditorFrame f;FakeView v;
 REQUIRE(f.install(w,&v));v.setFrame(&f);f.notify_scale();REQUIRE(v.scale==float(window_dpi(w))/96);
 REQUIRE(f.fit_reported());REQUIRE(v.attached(w,kPlatformTypeHWND)==kResultOk);f.attached=true;
 REQUIRE((client_size(w)==PixelSize{640,360}));REQUIRE(client_size(w)==client_size(v.child));
 ViewRect resize{5,2,775,446};
 REQUIRE(f.resizeView(&v,&resize)==kResultOk);REQUIRE((client_size(w)==PixelSize{770,444}));
 REQUIRE(v.changes==1);REQUIRE(client_size(w)==client_size(v.child));
 REQUIRE(f.resizeView(&v,&resize)==kResultOk&&v.changes==1);
 ViewRect bad{0,0,-10,0};
 REQUIRE(f.resizeView(&v,&bad)==kInvalidArgument);
 FakeView other;
 REQUIRE(f.resizeView(&other,&resize)==kInvalidArgument);
 v.reject=true;ViewRect rejected{0,0,455,330};
 REQUIRE(f.resizeView(&v,&rejected)==kResultFalse);REQUIRE((client_size(w)==PixelSize{770,444}));
 v.reject=false;
 const auto minOut=outer_rect(w,{600,340},f.dpi);
 RECT user{20,30,320,230};
 SendMessageW(w,WM_SIZING,WMSZ_TOPLEFT,reinterpret_cast<LPARAM>(&user));
 REQUIRE(v.checks==1&&user.right==320&&user.bottom==230);
 REQUIRE(user.right-user.left==minOut.right-minOut.left&&user.bottom-user.top==minOut.bottom-minOut.top);
 SendMessageW(w,WM_ENTERSIZEMOVE,0,0);
 SetWindowPos(w,nullptr,0,0,user.right-user.left,user.bottom-user.top,SWP_NOACTIVATE|SWP_NOMOVE|SWP_NOZORDER);
 // Some Win32 backends coalesce the hidden notification; deliver the same
 // event that will be sent for a visible drag to exercise the subclass.
 SendMessageW(w,WM_SIZE,SIZE_RESTORED,0);
 SendMessageW(w,WM_EXITSIZEMOVE,0,0);
 REQUIRE((client_size(w)==PixelSize{600,340}));REQUIRE(client_size(w)==client_size(v.child));
 v.resizable=false;
 RECT fixed{20,30,320,230};
 SendMessageW(w,WM_SIZING,WMSZ_BOTTOMRIGHT,reinterpret_cast<LPARAM>(&fixed));
 REQUIRE(fixed.right-fixed.left==minOut.right-minOut.left);
 v.request_on_scale=true;f.notify_scale();
 const auto mult=float(f.dpi)/96;
 REQUIRE((client_size(w)==PixelSize{int(std::lround(620*mult)),int(std::lround(350*mult))}));
 REQUIRE(client_size(w)==client_size(v.child));
 REQUIRE(!IsWindowVisible(w));v.removed();f.reset();DestroyWindow(w);
 std::cout<<"PASS: physical client vs outer frame, 96/144/192-DPI frame math, attach/resize state, native HWND child, user constraints, scale callback and hidden lifecycle\n";
}
