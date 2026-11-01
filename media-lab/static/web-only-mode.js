(() => {
  const init = async () => {
    try {
      const response = await fetch('/api/compute-mode', {cache: 'no-store'});
      if (!response.ok) return;
      const mode = await response.json();
      if (!mode.web_only) return;
      const banner = document.createElement('div');
      banner.id = 'media-lab-compute-mode';
      banner.setAttribute('role', 'status');
      banner.style.cssText = 'position:sticky;top:0;z-index:9999;padding:10px 16px;background:#332514;color:#ffe0a6;border-bottom:1px solid #7d6132;font:13px/1.45 system-ui;text-align:center';
      banner.textContent = 'GLM mode: Media Lab browsing and Library are online. Spark rendering is paused to protect GLM.';
      document.body.prepend(banner);
    } catch (_) { /* Existing UI remains available if the status probe fails. */ }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once:true});
  else init();
})();
