/* Presentation-only synthetic workflow. No OCR, network or production writes. */
(function (root) {
  const scenarios = {
    renewal: {name:'续期补档 · 正常路径', parcel:'DEMO-P001', annual:150000, old:240000, paid:180000, base:120000, issue:''},
    conflict: {name:'计租口径存疑', parcel:'DEMO-P001', annual:150000, old:240000, paid:180000, base:120000, issue:'计租方式未明确；示例方案 A 年租 1,500 元，方案 B 年租 1,800 元。取较低方案暂算并保留待确认事项。'},
    failure: {name:'模型不可用 · 转人工', parcel:'DEMO-P001', annual:150000, old:240000, paid:180000, base:120000, issue:'模拟连接超时；没有识别结果。人工录入并引用示例材料后才可继续。'},
    new: {name:'新宗地 · 人工建档', parcel:'DEMO-P002', annual:150000, old:0, paid:0, base:0, issue:'未匹配既有宗地。须核对来源后明确新建，不以承租人姓名自动合并。'}
  };
  function create(key='renewal') {if(!scenarios[key]) throw Error('未知场景');return {key,stage:'ready',role:'reviewer',annual:scenarios[key].annual,assigned:false,checks:[false,false,false],reason:'',logs:[],revision:0,notice:false,manual:false};}
  function amounts(s){const c=scenarios[s.key];const due=c.base+s.annual;return {due,paid:c.paid,arrears:Math.max(0,due-c.paid),oldArrears:Math.max(0,c.old-c.paid),delta:due-c.old,alternative:s.key==='conflict'?c.base+180000:null};}
  function event(s,action,detail){s.logs.push({seq:s.logs.length+1,action,detail,role:s.role,revision:s.revision});}
  function act(s,action,data={}){
    if(action==='role'){s.role=data.role==='viewer'?'viewer':'reviewer';return s;}
    if(s.role==='viewer')throw Error('只读角色不能执行此操作。此处为演示门禁，不是服务端认证。');
    const c=scenarios[s.key];
    if(action==='import'){
      if(!['ready','withdrawn'].includes(s.stage)){event(s,'重复拦截','相同示例材料标识已存在；有效台账与版本不增加。');return s;}
      s.stage=s.key==='failure'?'failed':'review';s.checks=[false,false,false];s.assigned=false;s.reason='';s.manual=false;
      event(s,s.revision?'重新导入':'导入材料',s.stage==='failed'?'模型模拟超时，保留原件，等待人工处理。':'装载预置示例候选，尚未生效；不代表真实 OCR。');
    } else if(action==='manual'){
      if(s.stage!=='failed')throw Error('仅失败材料可转人工');s.stage='review';s.manual=true;s.annual=0;event(s,'转人工','清空候选金额，需经办人员依据示例原件录入。');
    } else if(action==='edit'){
      if(s.stage!=='review')throw Error('仅待复核材料可修改');
      if(data.annual!==undefined){if(!Number.isInteger(data.annual)||data.annual<=0||data.annual>100000000)throw Error('请输入大于 0 且不超过 1,000,000 元的金额，最多两位小数');s.annual=data.annual;s.checks=[false,false,false];}
      if(data.assigned!==undefined)s.assigned=!!data.assigned;
      if(data.reason!==undefined)s.reason=data.reason;
      if(data.check!==undefined)s.checks[data.check]=!!data.value;
    } else if(action==='publish'){
      if(s.stage!=='review')throw Error('材料不处于待复核状态');
      if(!s.assigned||!s.checks.every(Boolean)||s.reason.trim().length<4||s.annual<=0)throw Error('请完成归宗、三项核验并填写至少 4 字的复核依据。');
      if(s.key==='conflict'&&s.annual!==150000)throw Error('本示例存疑口径固定为较低方案 1,500 元；其他值不能作为该示例暂算。');
      s.stage='published';s.revision++;event(s,'人工确认生效',`引用示例材料第 1 页；年租 ${s.annual/100} 元；依据：${s.reason.trim()}${c.issue&&s.key==='conflict'?'；存疑事项保留，待书面确认。':''}`);
    } else if(action==='notice'){
      if(s.stage!=='published')throw Error('尚无有效台账，不能生成草稿');s.notice=true;event(s,'生成催缴草稿','仅本页预览；没有发送短信或对外推送。');
    } else if(action==='withdraw'){
      if(s.stage!=='published')throw Error('仅有效材料可撤回');s.stage='withdrawn';s.notice=false;s.assigned=false;s.checks=[false,false,false];event(s,'撤回本次贡献','恢复补档前金额；关联催缴草稿失效；历史操作记录保留。');
    } else throw Error('未知操作');return s;
  }
  const api={scenarios,create,amounts,act};if(typeof module!=='undefined')module.exports=api;else root.RoadshowModel=api;
})(typeof window!=='undefined'?window:globalThis);
