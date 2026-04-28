# Claude baseline — "Compare wine making regions and traditions in the corpus"

**Generated:** 2026-04-27
**Method:** Claude (Opus 4.7) using Harbor Clerk MCP tools (`kb_corpus_overview`, `kb_search`, `kb_batch_search`, `kb_read_passages`) directly against the live Mac client knowledge base.
**Corpus stats:** 2,951 documents, 111,192 chunks, 29,799 pages (English: 99.9%, French: <1%). MIME mix is dominated by `.doc` files (2,383) — primarily wine-and-food magazine drafts.

This report is the reference target for evaluating local-LLM Research output on the same question. It is intentionally fact-dense rather than literary: the comparison criterion is **search completeness** — same regions, same producers, same factual handles — not stylistic quality.

## Corpus character

The corpus is dominated by editorial drafts for *The Art of Eating* (referred to in titles as `AoE`, `AoEcookbook-MS`, `AoE-79`/`-81`/`-82`/`-85`/`-86`/`-88`/`-89`) and standalone "Why This Bottle, Really?" wine columns. Many articles appear in 2–7 drafts (e.g. `outis 4`, `outis 4WY`, `outis 5`, `outis 5WY`; `jaugaret 1`/`1WY`/`3`/`3WY`/`5WY`/`7`; `muscadet 1`/`5`/`6-16`/`7-1-10`/`7-20DES`; `madeira 4`/`5DS`/`7`/`11`/`14EB`/`14WY`). A bilingual or "WY" suffix likely marks a particular editorial pass. **An LLM that reports a region only once per draft is fine; one that double-counts each draft as a separate source is not.** A small minority of documents are classic-text Tika ingests (Rousseau, Spinoza, Kant, Descartes, Bacon, Augustine, Montaigne, Lucretius, Aristotle) — these surface incidentally on wine queries and should be filtered out of any region report.

## Regions, traditions, and headline facts

For each region the bullets list: **producer / wine** — **grape(s)** — **distinctive practice or fact** — `[doc title, page]` for at least one citation a reader can verify.

### France

- **Bordeaux — Médoc — Château Jaugaret.** Cabernet Sauvignon and Merlot vines averaging 60+ years old; ~100-year-old Malbec; total output 3,000–6,000 bottles/year. Wine is unlike the rest of the Médoc: rustic, austere, tannic, and reads more like Burgundy than like Pauillac/Margaux/St-Estèphe. *[`jaugaret 7`, pp1–2; `jaugaret1WY`, pp1–2]*
- **Burgundy — Chablis (Domaine reviewed in `AoE-81`).** Kimmeridgian-marl soils; Portlandian "can give very good wines… but the capacity for aging is not there." Enologist Didier Séguier (ex-Bouchard Père et Fils, Henriot). Outdoor regime is *lutte raisonné* — 15 ha effectively organic but uncertified. New-oak use has been reduced under new ownership; wines now read more "Chablis." *[`AoE-81-Text Sigs`, p18]* The corpus also references "L2O" Burgundy material (`L2O 9`/`9_wsr`/`10`/`10WY`).
- **Loire — Muscadet-Sèvre et Maine.** Grape: Melon de Bourgogne (Jancis Robinson: "not a noble grape"). Soils: Massif Armoricain — Precambrian granite, metamorphic schist, gneiss, and gabbro (alkaline volcanic rock similar to basalt). AOCs: Muscadet de Sèvre et Maine and Muscadet Coteaux de la Loire (1936–37); generic Muscadet (1937). Strong "traditional vs modern" tension in the appellation. *[`muscadet6-16WY`, pp2–3; `muscadet-1`, pp12–13; `AoE-85July30`, p13 (David Lillie, "The Flavor of Stone")]*
- **Loire — Bourgueil & St-Nicolas-de-Bourgueil — Yannick Amirault.** Cabernet Franc; sandy/gravelly soils for the lighter wines, clayey-limestone/silica slopes for the more serious cuvées. Seven single-vineyard reds across 19 ha in Bourgueil + 6 ha in St-Nicolas. *[`amirault1`, p1]*
- **Northern Rhône — Syrah traditionalists.** "Velvety, smoked-meat scented reds" cited as the reference style for Syrah. *[`edmunds 3WY`, p1; `No 83-text`, p50]*
- **Champagne — grower-Champagne movement.** Featured: José Dhondt (Blanc de Blancs, Brut, ~$56). Other growers explicitly named as worth seeking: Larmandier-Bernier, Egly-Ouriet, Vilmart & Cie, Pierre Gimonnet, Camille Savès, Cédric Bouchard. Grandes marques still respected: Krug, Charles Heidsieck, Bollinger, Louis Roederer, Taittinger. *[`AoE-86-Text Sigs`, pp51–52]*
- **Provence — Bandol — Domaine Tempier.** Cited specifically in the context of red-wine *aging tanks* (vs the modern barrique norm). *[`AoE-89-Text`, pp33–34]*

### Italy

- **Sicily — Etna — Azienda Biondi.** Volcanic terroir with parcels on pure black sand at Monte Ilice and Carpene that "heightens the wine's mineral depth." *Outis* cuvée: Nerello-based, fashioned in oak barrels and 500-liter tonneaux purchased from Planeta. Enologist Salvo Foti initially; Biondi himself from 2006. The region "has no real continuity of style" — explicitly *not* a tradition. Same article calls out **Frank Cornelissen** (Belgian, makes Nerello-based wines using amphorae and minimal technology). *[`outis 5`, pp3–4; `outis 4WY`, pp3–4; `outis 4`, p2]*
- **Tuscany — Brunello di Montalcino — Il Poggione.** Sangiovese Grosso (the Brunello clone), developed late 19th c. by a Tuscan noble; smaller berries → higher skin-to-flesh ratio → richer color/tannin/structure. Southwestern subzone of Montalcino is shielded from southern winds by Mt. Amiata, kissed by Tyrrhenian-sea breezes. *[`poggione 1WY`, p1]*
- **Piedmont — Barolo — Cannubi Hill, Ristorante I Cannubi.** Cannubi Hill is "famous throughout the Piedmont for birthing some of the finest Nebbiolo grapes in the region." Other docs in the corpus: `piemontese`, `piemontese 2`, `piemontese_start`, `piemontese_startEB`. *[`Food Writing Sample 42 pgs`, p4]*
- **Italian winemaking generally — barrique vs botte.** The barrique era in Italy began following Robert Mondavi/Napa imports of French practice in the 1960s+; previously, much California *and* Italian aging was in stainless tanks. Bordeaux enologist **Pascal Chatonnet** (oak specialist) is cited on barrique uptake. *[`AoE-89-Text`, pp33–34]* The corpus also includes a `big-oak-casks` series (`big-oak-casks 4`/`5`) on the Slavonian-style large-cask tradition.

### Spain

- **Rioja Alta — Bodegas Hermanos Peciña — Señorío de P. Peciña Reserva (~$30).** Founder Pedro Peciña Crespo, 20-year veteran of Grupo La Rioja Alta, founded the winery in 1992 with 50 ha. Rioja Alta produces the lightest-bodied of the appellation's three subzones (higher elevation, cooler nights, shorter season). Tempranillo is the principal grape; soil is yellowish clay over limestone. *[`senorio 13`, p1]* Multiple drafts: `senorio 1WY`/`9`/`11WY`/`12_wsr`/`13`.
- **Andalucía — Jerez — fino sherry.** Featured: **fino Inocente** (multiple drafts: `fino inocente 1WY`/`1WY-EB`/`3`/`3WY`). The corpus also has `aoe88-winerevs` and `AoE-88-Text Sigs` (pp40–67) coverage of fino. (Specific solera/flor details not extracted in this baseline pass.)

### Portugal

- **Madeira.** Wine often crossed to the New World; Madeira often made up "part or all" of a ship's cargo bound for America. Until the early 1700s "a cheap, simple wine, made from a base of white grape must to which growers and exporters added varying amounts of red must." Mid-1700s: fortification with brandy (now 18–20% ABV) extended shelf life — this kills microbes (notably acetobacters), and the extra alcohol "added body and smoothed out an otherwise harsh product." Reference work cited: David Hancock, *Oceans of Wine*. *[`madeira 5DS`, pp1–2; `madeira 7`, pp1–2]* Multiple drafts: `madeira 4`/`5DS`/`7`/`11`/`11clean`/`14EB`/`14WY`.

### Germany

- **Mosel — Christoffel — Ürziger Würzgarten Riesling Kabinett.** Featured in *AoE-85*'s "Why This Bottle, Really?" by Belinda Chang. Same issue features Levi Dalton on **Fiano di Avellino (Campania, Pietracupa)** — also Italy. *[`AoE-85July30`, pp2–3]* Multiple Christoffel drafts: `Christoffel Urziger Wurzgarten Kabinett 2008` (×2), `christoffel-6-11`. There is also a dedicated `mosel` document and a `Krieger` essay (`Essay_Krieger_211010-2`).

### "New world"

- **California — Edmunds St. John (Steve Edmunds), El Dorado County — 2005 Wylie-Fenaughty Syrah (~$25).** Made in the Northern-Rhône traditionalist mold; called "California's truest Syrahs" and characterized as "purist." California Syrah has surged in plantings (#2 in new plantings after Pinot Noir) but is shunned by consumers. *[`edmunds 3WY`, p1; `No 83-text`, p50]*
- **California — Tablas Creek Winery, Paso Robles.** Photo caption — fermentation tanks alongside Domaine Tempier (Bandol) — frames Tablas Creek as a Rhône-style producer. *[`AoE-89-Text`, pp33–34]*
- **California climate effect (terroir).** A recurring article (`terroir-draft-1`, `terroir 1 WY`, `terroir 3`, `terroir-6-dfs`, `terroirDES 11`) explains why a California Cabernet is darker than a French Bordeaux: longer skin contact + naturally higher anthocyanins from warmer days; warmer climates lower methoxypyrazines, so cool-climate Cabernet Franc (Loire, Long Island) shows more bell-pepper character than California's. California vintners "often add tartaric acid to their wines, much as many European vintners add sugar to the crushed grapes" (i.e. acidification vs chaptalization). *[`terroir-draft-1`, pp3–6; `AoE 82`, p31]*
- **Oregon — biodynamic Pinot/Chardonnay (review of Katherine Cole's *Voodoo Vintners*).** Producer reviewed: a domaine making 2009 Cascadia Chardonnay, 2009 Cuvée du Tonnelier Pinot Noir, 2009 Les Dijonnaise Pinot Noir — described as "the most Burgundian, offering purity and understated complexity." *[`Book Review of Voodoo Vintners version 2`, p3]*
- **Australia — Yarra Valley — Yarra Yering (Dr. Bailey Carrodus).** Vineyard >40 years old; gray loam and silty lime over free gravel; north-facing slopes. Grapes: Chardonnay, Shiraz (= Syrah), Pinot Noir, plus port varieties added in the 1990s — Touriga Naçional, Tinta Cão — terraced "emulating Douro winemakers." Dry Red No.1 (Bordeaux blend, 12.5% ABV) peaks at 10–15 years; Dry Red No.3 uses Portuguese varieties. Style: minimal-intervention — little/no fining, filtering, pumping over, added acid or sulfur, and little/no new oak. *[`Yarra-4-30`, pp1–9; `yarra-4-9`, p2]* Multiple drafts: `Yarra Valley 8WY-EB`, `YarraValley 3WY`, `Yarra Valley 10`, `Yarra Valley 11`.
- **New Zealand — Waiheke / Man O'War.** Multiple drafts (`ManoWar 2WY`, `ManoWar 4`, `ManoWar_3_wsr`). (Specific cuvée/grape details not extracted in this baseline pass.)

### Slovenia / North-East Italy

- **Goriška Brda / Friuli — Kabaj — Rebula (= Ribolla Gialla).** Grape grown in the region since the 13th century. Wine carries 13% ABV with "vigorous energy" and a "stately mouthfeel" — the article compares the body (not flavor) to a properly aged Bordeaux red. Kabaj wines explicitly described as "remarkably stable." *[`rebula 5 joe`, pp2–3]* Related drafts in the corpus: `channing 6WY-EB`, `channing 6-MA`.

## Cross-cutting traditions

The corpus does **not** present "regions" as discrete tradition packages — it presents *recurring stylistic and technical axes* that cross regions. The most prominent:

1. **Traditional vs modern winemaking.** The dominant editorial frame, called out in nearly every region article: stainless steel + neutral oak + minimal intervention vs new-French-oak barriques + extraction + selected yeasts. Specifically articulated in the Muscadet series (`muscadet-7-20DES`, p8; `muscadet6-16WY`, p6 — both score 1.50+ on a "traditional modern winemaking" search) and the Etna pieces ("you can't call Biondi's wines traditional, for the Etna region has no real continuity of style").
2. **Barrique uptake from Bordeaux/Napa.** Italy and California aged in stainless tanks well into the 1970s. The shift to French barrique followed Mondavi-Bordeaux exchange in the 1960s. Pascal Chatonnet (Bordeaux enologist, oak specialist) cited on the decline. *[`AoE-89-Text`, pp33–34]*
3. **Big-oak-cask (botte) tradition.** A whole series (`big-oak-casks 1–5`) treats the older Italian/Slavonian large-cask élevage as a counterpoint to the barrique era.
4. **Natural / biodynamic.** *Voodoo Vintners* (Katherine Cole) review, Frank Cornelissen on Etna with amphorae and "the barest of technologies," Yarra Yering's no-fining/no-filtering regime, and the lutte-raisonné mention at the Chablis domaine. The corpus has 65 hits on "natural wine biodynamic" — the strongest cluster around `jaugaret`, `Voodoo Vintners`, and `muscadet`.
5. **Amphora / qvevri / orange wine.** 30 hits. Strongest concentration in the `muscadet-1`/`muscadet-2WY-1-1`/`muscadet-7-20DES`/`muscadet 3` cluster; also Cornelissen on Etna and Kabaj/Rebula in Friuli/Slovenia. (No dedicated Georgia / Kakheti documents surfaced.)
6. **Terroir as a narrative.** The corpus has explicit `terroir`-titled articles (`terroir-draft-1`, `terroir 1 WY`, `terroir 3`, `terroir-6-dfs`, `terroirDES 11`) treating climate effects on grape chemistry (anthocyanins, methoxypyrazines, acidity vs chaptalization).
7. **Grower vs négociant Champagne.** The Dhondt feature explicitly names the grower-Champagne movement and contrasts it (without rejecting) with the grandes marques.
8. **Fortification & shipping.** Madeira's fortification-with-brandy story (1700s) is the corpus's clearest treatment of fortification as a *tradition* — both as preservation and flavor-shaping.

## Regions/traditions the corpus does **not** materially cover

Confirmed absent or trivial (top hits are unrelated philosophical texts, philosophy classics, or off-topic articles):

- South America — Argentina/Malbec, Chile (top hit `Cheesemaking`).
- South Africa.
- Greece, Hungary/Tokay.
- Austria/Grüner Veltliner (top hit `foreignsubs` — a subscriber list).
- Republic of Georgia / Kakheti qvevri tradition (the amphora coverage in the corpus is Etna and Friuli, not Georgia).
- Beaujolais — searches on "Beaujolais Gamay" and "Beaujolais Cru Morgon Fleurie" surface `coates` documents and unrelated texts; Beaujolais is not directly featured in the way Bordeaux/Burgundy/Loire/Rhône are.

A correct local-LLM report should either omit these regions or note their absence — fabricating Argentine/Greek/Georgian content would be a hallucination.

## Comparison rubric (for evaluating LLM outputs)

When comparing the local LLMs (Gemma 4 26B-A4B, GPT-OSS 20B) to this baseline, score on:

- **Regions named** — should hit at least: Bordeaux (Médoc/Jaugaret), Burgundy/Chablis, Loire (Muscadet, Bourgueil), Northern Rhône, Champagne, Bandol/Provence, Sicily/Etna, Tuscany/Montalcino, Piedmont/Barolo, Mosel, Rioja, Madeira, California (Edmunds, Tablas Creek), Yarra Valley, New Zealand (Waiheke/Man O'War), Friuli/Slovenia.
- **Producers cited** — Jaugaret, Yannick Amirault, Biondi, Cornelissen, Il Poggione, Christoffel, Bodegas Peciña, Edmunds St. John, Tablas Creek, Yarra Yering, José Dhondt, Domaine Tempier, Kabaj.
- **Distinctive facts** — Médoc/Burgundy comparison for Jaugaret, Mt. Amiata shielding Brunello, Massif Armoricain for Muscadet, Mondavi-Bordeaux barrique provenance, Madeira fortification 18–20% ABV, Sangiovese Grosso clone, Rioja Alta subzone characteristics.
- **Cross-cutting themes** — traditional vs modern, barrique uptake, natural/biodynamic, grower-Champagne, terroir/climate chemistry.
- **Citation discipline** — references should resolve to real document titles and pages. Drafts (`-WY`, `-EB`, numeric suffix) of the same article should not be counted as independent sources.
- **Negative coverage** — does the model refrain from claiming Argentine/Greek/Georgian content that isn't in the corpus?

## Notes on baseline construction (not part of the comparison)

Calls used to build this baseline (visible in `/api/api-keys/{key}/usage/requests` for `claude-baseline-mcp-temp`):

- 1× `kb_corpus_overview`
- ~10× `kb_search` (single-region targeted)
- 4× `kb_batch_search` (5-query batches across regions and traditions)
- 6× `kb_read_passages` (multi-chunk reads for verification)

The temp API key (`claude-baseline-mcp-temp`, `key_id` 438349c9-…) should be revoked when this debugging task is closed. The token file lives at `/tmp/hc-research-debug/api-key` (not committed).
