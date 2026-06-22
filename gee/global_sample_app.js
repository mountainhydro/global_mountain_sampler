// Global Mountain Sampling — GEE App
// Shows the stratified AlphaEarth sample points over the GMBA mountain regions.
// The GMBA layer is drawn as a faint fill + thin outline so the base map stays
// clearly visible underneath.
//
// Asset produced by notebooks/global_sampling.ipynb (section 8):
//   projects/.../assets/global_mountain_sample_1000   ← 1000 k-means medoids

var ASSET_ROOT   = "projects/promising-era-496715-j5/assets";
var SAMPLE_ASSET = ASSET_ROOT + "/global_mountain_sample_1000";   // 1000 k-means medoids
var GMBA_ASSET   = ASSET_ROOT + "/GMBA_Inventory_standard_300";

// Pre-rendered climate-space density plots, ingested as GEE image assets
// (notebooks/global_climate_space.ipynb section 5/6). Each {x}__{y} is an RGB
// hexbin density + medoids plot; bands b1,b2,b3. Both orientations exist (110).
var PLOT_BASE = ASSET_ROOT + "/climate_space_plots/";
var CVARS = ["tas", "tasmax", "tasmin", "pr", "pet", "cmi",
             "hurs", "clt", "rsds", "sfcWind", "vpd"];
// Full names shown in the dropdowns; the abbreviation stays as the value (used
// to build the asset name {x}__{y}).
var CNAME = {
  tas: "Mean annual air temp", tasmax: "Max temperature", tasmin: "Min temperature",
  pr: "Annual precipitation", pet: "Potential evapotranspiration", cmi: "Climate moisture index",
  hurs: "Relative humidity", clt: "Cloud cover", rsds: "Shortwave radiation",
  sfcWind: "Wind speed", vpd: "Vapour pressure deficit",
};
var CVAR_ITEMS = CVARS.map(function (v) { return {label: CNAME[v], value: v}; });

// Supersite basins (uploaded by global_climate_space.ipynb). Same colours are
// used for the 90% hulls baked into the climate-space plots.
var SUP_ASSET = ASSET_ROOT + "/supersites";
var SITES = [
  {name: "Pamir",     color: "#000000"},
  {name: "Riosanta",  color: "#3cb043"},
  {name: "Vilcanota", color: "#cc00cc"},
  {name: "Trisuli",   color: "#8c510a"},
];

// Kapos elevation-class palette (K1 highest … K6 lowest)
var KAPOS_COLORS = ["#54278f", "#2171b5", "#08519c", "#1a9641", "#fee08b", "#f46d43"];
var KAPOS_LABELS = [
  "K1  > 4500 m",
  "K2  3500–4500 m",
  "K3  2500–3500 m",
  "K4  1500–2500 m",
  "K5  1000–1500 m",
  "K6  300–1000 m",
];

// ── Data ────────────────────────────────────────────────────────────────────
var gmba   = ee.FeatureCollection(GMBA_ASSET);
var sample = ee.FeatureCollection(SAMPLE_ASSET);

// ── Base map ────────────────────────────────────────────────────────────────
// TERRAIN keeps the relief readable beneath the overlays.
Map.setOptions("TERRAIN");
Map.setCenter(20, 25, 3);
Map.setControlVisibility({layerList: true});

// ── GMBA regions: faint fill only (base map shows through) ───────────────────
var gmbaFill = ee.Image().byte().paint(gmba, 1);          // interior
Map.addLayer(gmbaFill, {palette: ["#1f3b73"]}, "GMBA regions", true, 0.3);

// ── Supersite basins: coloured outlines (match the climate-space hull colours) ─
var supersites = ee.FeatureCollection(SUP_ASSET);
SITES.forEach(function (s) {
  var fc  = supersites.filter(ee.Filter.eq("site", s.name));
  var out = ee.Image().byte().paint(fc, 1, 2);           // 2-px outline
  Map.addLayer(out, {palette: [s.color]}, "Supersite: " + s.name, true);
});

// ── Sample points, coloured by Kapos class ──────────────────────────────────
// Filled circles coloured by class, one layer per class.
for (var i = 0; i < 6; i++) {
  var cls = i + 1;
  var styled = sample.filter(ee.Filter.eq("kapos_class", cls)).style({
    color:     "303030",              // outline
    fillColor: KAPOS_COLORS[i],       // fill
    pointSize: 4,
    pointShape: "circle",
    width:     1,
  });
  Map.addLayer(styled, {}, "Sample " + KAPOS_LABELS[i], true);
}

// ── Legend (top-right) ──────────────────────────────────────────────────────
var legend = ui.Panel({style: {position: "top-right", padding: "8px 12px"}});
legend.add(ui.Label("Sample points — Kapos class",
  {fontWeight: "bold", fontSize: "13px", margin: "0 0 6px 0"}));
KAPOS_LABELS.forEach(function (lbl, i) {
  legend.add(ui.Panel({
    widgets: [
      ui.Label("", {backgroundColor: KAPOS_COLORS[i], padding: "7px",
                    margin: "2px 6px 2px 0", border: "1px solid #333"}),
      ui.Label(lbl, {fontSize: "12px", margin: "2px 0"}),
    ],
    layout: ui.Panel.Layout.flow("horizontal"),
  }));
});
legend.add(ui.Panel({
  widgets: [
    ui.Label("", {backgroundColor: "rgba(31,59,115,0.5)", padding: "7px",
                  margin: "6px 6px 2px 0", border: "1px solid #1f3b73"}),
    ui.Label("GMBA mountain region", {fontSize: "12px", margin: "6px 0 2px 0"}),
  ],
  layout: ui.Panel.Layout.flow("horizontal"),
}));
legend.add(ui.Label("Supersites", {fontWeight: "bold", fontSize: "12px", margin: "6px 0 2px 0"}));
SITES.forEach(function (s) {
  legend.add(ui.Panel({
    widgets: [
      ui.Label("", {backgroundColor: s.color, padding: "7px",
                    margin: "2px 6px 2px 0", border: "1px solid #333"}),
      ui.Label(s.name, {fontSize: "12px", margin: "2px 0"}),
    ],
    layout: ui.Panel.Layout.flow("horizontal"),
  }));
});
Map.add(legend);

// ── Climate-space comparison (bottom-left): two plots, each with X/Y dropdowns ─
// Pre-rendered hexbin-density plots ingested as image assets; X/Y maps straight
// to the asset {x}__{y} (both orientations exist), shown as RGB (bands b1,b2,b3).
function pairKey(a, b) {
  return a + "__" + b;
}

function makeClimatePlot(defX, defY) {
  var st    = {x: defX, y: defY};
  var thumb = ui.Thumbnail({
    image:  ee.Image(0),
    params: {bands: ["b1", "b2", "b3"], min: 0, max: 255, dimensions: 430, format: "png"},
    style:  {height: "260px", margin: "2px 0"},
  });
  var note = ui.Label("", {fontSize: "10px", color: "#a00", margin: "0 0 0 4px"});

  function refresh() {
    if (st.x === st.y) { note.setValue("pick two different variables"); return; }
    note.setValue("");
    thumb.setImage(ee.Image(PLOT_BASE + pairKey(st.x, st.y)));
  }

  var selX = ui.Select({items: CVAR_ITEMS, value: defX, style: {width: "150px", fontSize: "11px"},
                        onChange: function (v) { st.x = v; refresh(); }});
  var selY = ui.Select({items: CVAR_ITEMS, value: defY, style: {width: "150px", fontSize: "11px"},
                        onChange: function (v) { st.y = v; refresh(); }});
  refresh();

  return ui.Panel(
    [ui.Panel([ui.Label("X", {fontSize: "11px", margin: "4px 3px 0 0"}), selX,
               ui.Label("Y", {fontSize: "11px", margin: "4px 3px 0 6px"}), selY, note],
              ui.Panel.Layout.flow("horizontal")),
     thumb],
    ui.Panel.Layout.flow("vertical"),
    {border: "1px solid #bbb", margin: "0 4px 0 0", padding: "4px", width: "360px"});
}

Map.add(ui.Panel({
  widgets: [
    ui.Label("Climate space — density plot + 1000pts sample + supersites 90% KDE hulls",
      {fontWeight: "bold", fontSize: "12px", margin: "0 0 4px 0"}),
    ui.Panel([makeClimatePlot("tas", "pr"), makeClimatePlot("pr", "tasmin")],
             ui.Panel.Layout.flow("horizontal")),
  ],
  style: {position: "bottom-left", padding: "6px 8px",
          backgroundColor: "rgba(255,255,255,0.93)"},
}));
