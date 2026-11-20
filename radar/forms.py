from django import forms


REVIEW_CHOICES = [
    ("new", "尚未复核"),
    ("accepted", "值得跟进"),
    ("needs_info", "需要补充信息"),
    ("not_fit", "暂不匹配"),
    ("contacted", "已实际跟进"),
]
OWNERSHIP_CHOICES = [
    ("unknown", "背景待核实"),
    ("japan_confirmed", "日资背景已核实"),
    ("foreign_confirmed", "其他外资背景已核实"),
    ("domestic_confirmed", "内资背景已核实"),
]


class ReviewForm(forms.Form):
    status = forms.ChoiceField(label="复核结果", choices=REVIEW_CHOICES)
    note = forms.CharField(
        label="复核记录", required=False, max_length=3000,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "记录匹配原因、待补信息或实际跟进动作。"}),
    )

    def clean(self):
        data = super().clean()
        if data.get("status") == "contacted" and not data.get("note", "").strip():
            self.add_error("note", "记录已实际跟进时，请写明已经完成的动作。")
        return data


class CompanyForm(forms.Form):
    buyer_name = forms.CharField(label="实际采购企业", max_length=240,
                                widget=forms.TextInput(attrs={"placeholder": "填写完整采购法人名称"}))
    ownership_status = forms.ChoiceField(label="企业背景", choices=OWNERSHIP_CHOICES)
    ownership_evidence = forms.CharField(
        label="背景依据", required=False, max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "填写核验来源、网址和判断依据；名称或品牌本身不能证明股权背景。"}),
    )

    def clean_buyer_name(self):
        return " ".join(self.cleaned_data["buyer_name"].split())

    def clean(self):
        data = super().clean()
        if data.get("ownership_status") not in (None, "unknown") and not data.get("ownership_evidence", "").strip():
            self.add_error("ownership_evidence", "确认企业背景时必须记录核验依据。")
        return data


class ImportForm(forms.Form):
    title = forms.CharField(label="公告标题", max_length=500,
                            widget=forms.TextInput(attrs={"placeholder": "输入完整标题"}))
    url = forms.URLField(label="原始链接", required=False, max_length=2000, assume_scheme="https",
                         widget=forms.URLInput(attrs={"placeholder": "https://…"}))
    source_kind = forms.ChoiceField(label="信息类别", choices=[
        ("procurement", "采购公告 / 供应商招募"),
        ("early_signal", "扩产 / 搬迁等早期信号"),
    ])
    text = forms.CharField(label="原文内容", max_length=200000, min_length=20,
                           widget=forms.Textarea(attrs={"rows": 15, "placeholder": "粘贴可以合法使用的公告正文。尽量包含采购方、服务范围、日期和参加方式。"}))

    def clean_url(self):
        url = self.cleaned_data.get("url", "")
        if url and not url.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("原始链接仅支持 http 或 https。")
        return url


class SourceSettingsForm(forms.Form):
    head_pages = forms.IntegerField(label="每轮检查前几页", min_value=1, max_value=30, required=False)
    max_items = forms.IntegerField(label="每批详情上限", min_value=1, max_value=50, required=False)
    max_accepted_items = forms.IntegerField(label="本轮最多保存线索", min_value=1, max_value=50, required=False)
    max_detail_requests = forms.IntegerField(label="本轮详情请求上限", min_value=1, max_value=200, required=False)
    max_total_requests = forms.IntegerField(label="本轮访问次数上限", min_value=1, max_value=500, required=False)
    max_elapsed_seconds = forms.IntegerField(label="本轮最长秒数", min_value=1, max_value=600, required=False)
    keywords = forms.CharField(label="东风公开查询词（逗号分隔）",max_length=500,required=False)
    enabled = forms.BooleanField(label="启用定期采集", required=False)
    interval_minutes = forms.IntegerField(label="采集间隔（分钟）", min_value=60, max_value=10080)

from .models import AIConfig, BusinessCapability
from .llm import _completion_url

class AISettingsForm(forms.ModelForm):
    api_key = forms.CharField(label='替换模型密钥（留空保持）',required=False,widget=forms.PasswordInput(render_value=False),max_length=1000)
    delete_key = forms.BooleanField(label='清除模型密钥',required=False)
    search_api_key = forms.CharField(label='Brave Search 密钥（可选）',required=False,widget=forms.PasswordInput(render_value=False),max_length=1000)
    delete_search_key = forms.BooleanField(label='删除已保存的检索密钥',required=False)
    class Meta:
        model=AIConfig
        fields=['enabled','auto_new','base_url','model','deep_model','pro_thinking','daily_limit','batch_limit','max_tokens','input_chars','timeout_seconds','search_enabled','search_daily_limit']
        labels={'enabled':'启用模型手动分析','auto_new':'自动分析新采集的公开公告（不重跑历史）','base_url':'Chat Completions Base URL','model':'常规模型（MiMo 默认 Flash）','deep_model':'手动深度研判模型','pro_thinking':'启用 Pro 标准思考模式','daily_limit':'每日模型请求上限（失败/连接测试也计数）','batch_limit':'单批任务上限','max_tokens':'单次最大输出 token','input_chars':'单次证据字符预算','timeout_seconds':'请求超时秒数（不自动重试）','search_enabled':'启用 Brave Search 主体检索','search_daily_limit':'每日搜索请求上限（独立统计）'}
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['pro_thinking'].help_text = '两段式 Pro 营业军师简报始终使用标准思考模式。此开关适用于旧版深度任务。'
        self.order_fields(["enabled","auto_new","base_url","model","deep_model","pro_thinking","api_key","delete_key","daily_limit","batch_limit","max_tokens","input_chars","timeout_seconds","search_enabled","search_api_key","delete_search_key","search_daily_limit"])
    def clean_base_url(self):
        value=self.cleaned_data['base_url'].strip()
        if value:
            try:_completion_url(value)
            except ValueError:raise forms.ValidationError('请输入 HTTPS 地址；仅本机回环允许 HTTP，不能包含账号、查询参数或片段。')
        return value
    def clean(self):
        data=super().clean()
        if 'deep_model' not in self.data:
            data['deep_model']=self.instance.deep_model
        if data.get('model','').lower().startswith('mimo-') and data['model'] not in {'mimo-v2.6-flash','mimo-v2.6-pro'}:
            self.add_error('model','仅支持标准档，超高速档不可用。')
        for field,low,high in [('daily_limit',0,1000),('batch_limit',1,30),('max_tokens',128,8000),('input_chars',2000,80000),('timeout_seconds',5,120),('search_daily_limit',0,100)]:
            if data.get(field) is not None and not low<=data[field]<=high:self.add_error(field,f'允许范围 {low}–{high}')
        if data.get('enabled') and (not data.get('base_url') or not data.get('model')):
            self.add_error('enabled','启用前请填写地址和模型。')
        return data

class CapabilityForm(forms.ModelForm):
    class Meta:
        model=BusinessCapability
        fields=['regions','services','industries','qualifications','exclusions']
        labels={'regions':'可服务区域','services':'实际可承接的物流服务','industries':'已具备的行业经验','qualifications':'现有资质','exclusions':'不能承接的条件'}
        widgets={k:forms.Textarea(attrs={'rows':3}) for k in fields}
