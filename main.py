// ===== 일괄 분석 (서버가 최대 10개를 동시에 처리) =====
const BMAX = 10;
let batchRows = [];
function drawBatch(){
  $("bout").innerHTML = `<table class="mx" style="width:100%"><tr><th>입력</th><th>찾은 앱</th><th>AI 가능성</th><th>완성도</th><th>우선순위</th><th>판정</th><th></th></tr>` +
    batchRows.map((r,i) => {
      const d = r.data;
      if(!d || d.error) return `<tr><td>${esc(r.query)}</td><td colspan="6" class="small">실패: ${esc(d ? d.error : "")}</td></tr>`;
      return `<tr><td>${esc(r.query)}</td><td>${esc(d.name)}</td><td>${esc(d.ai_score)}</td><td>${d.quality_score==null?"-":esc(d.quality_score)}</td><td><b>${esc(d.priority)}</b></td><td>${esc(d.triage)}</td><td><button class="ghost" data-bi="${i}">상세</button></td></tr>`;
    }).join("") + `</table>`;
}
$("bgo").addEventListener("click", async () => {
  const lines = [...new Set($("bq").value.split("\n").map(x=>x.trim()).filter(Boolean))];
  if(!lines.length){ $("bq").focus(); return; }
  if(lines.length > BMAX){ $("bmsg").textContent = `한 번에 ${BMAX}개까지만 가능해요. (지금 ${lines.length}개)`; return; }
  $("bgo").disabled = true;
  $("bout").innerHTML = "";
  $("bmsg").textContent = `${lines.length}개 분석 중… 최대 1분 정도 걸려요. 탭을 닫지 마세요.`;
  try{
    const res = await fetch("/api/batch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({queries:lines})});
    const txt = await res.text();
    let j; try{ j = JSON.parse(txt); }catch(_){ j = {error:`서버 응답 오류(${res.status}). 시간 초과일 수 있어요. 개수를 줄여 다시 시도해 보세요.`}; }
    if(j.error){ $("bmsg").textContent = j.error; }
    else{
      batchRows = j.items;
      drawBatch();
      $("bmsg").textContent = `${j.items.length}개 완료 · 우선순위 높은 순으로 정렬했어요`;
    }
  }catch(e){ $("bmsg").textContent = "요청에 실패했어요: " + e.message; }
  $("bgo").disabled = false;
});
$("bout").addEventListener("click", e => {
  const b = e.target.closest("button[data-bi]"); if(!b) return;
  const r = batchRows[Number(b.dataset.bi)];
  if(r && r.data){ render(r.data); $("out").scrollIntoView({behavior:"smooth"}); }
});
