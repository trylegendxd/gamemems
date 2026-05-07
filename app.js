// ──────────────────────────────────────────────────────────────────────────
// PATCH: static/app.js
// Adicionar dentro da função  bindFilters()  ou no bloco de boot, após
// o listener de  #btn-run-now.
// ──────────────────────────────────────────────────────────────────────────

  const btnLimpar = document.getElementById("btn-limpar-anuncios");
  if (btnLimpar) {
    btnLimpar.addEventListener("click", async () => {
      const confirmed = window.confirm(
        "⚠️ Tens a certeza?\n\n" +
        "Esta acção apaga TODOS os anúncios guardados e reseta o índice de\n" +
        "deduplicação. O próximo scan volta a publicar tudo como se fosse\n" +
        "a primeira vez.\n\n" +
        "Confirmar limpeza?"
      );
      if (!confirmed) return;

      btnLimpar.disabled = true;
      btnLimpar.textContent = "A limpar…";

      try {
        const r = await fetch("/api/admin/limpar-anuncios", {
          method: "POST",
          headers: { "X-Confirm-Action": "limpar" },
        });
        const j = await r.json();

        if (!r.ok) {
          alert(`Erro: ${j.detail || j.error || r.status}`);
          return;
        }

        const msg =
          `✅ Limpeza concluída!\n\n` +
          `Anúncios removidos:  ${j.db.deals}\n` +
          `Registos de dedup:   ${j.db.seen_listings}\n` +
          `seen.json:           ${j.local_caches.seen_file_cleared ? "limpo" : "erro"}\n` +
          `market_cache.json:   ${j.local_caches.market_cache_file_cleared ? "limpo" : "erro"}`;
        alert(msg);

        // Actualizar o dashboard imediatamente
        await refreshAll();
      } catch (e) {
        alert(`Erro de rede: ${e.message}`);
      } finally {
        btnLimpar.disabled = false;
        btnLimpar.textContent = "🗑 Limpar anúncios";
      }
    });
  }
