"""Search hints, never proof of company ownership or an actual logistics purchase."""

# Brand matches are deliberately scoped to the identified organization name.
CORE_BRANDS = (
    '爱信', '電裝', '电装', '村田', '京瓷', '东丽', '東麗', '旭化成', '欧姆龙', '歐姆龍',
    '发那科', '發那科', '岛津', '島津', '日本电产', '日本電產', '矢崎', '住友',
    '丰田', '豐田', '本田', '日产', '日產', '松下', '索尼', '资生堂', '資生堂',
    '花王', '佳能', '理光', '兄弟', '小松', '日立', '三菱', '三井', '丸红', '丸紅',
    '伊藤忠', '双日', '雙日', '豊田通商', '丰田通商', '日通', '近铁', '近鐵', '邮船', '郵船', '山九', '丸全昭和', 'Yusen', 'KWE', 'NX', 'Nippon Express',
)
JOINT_BRANDS = ('广汽丰田', '廣汽豐田', '一汽丰田', '一汽豐田', '东风日产', '東風日產',
                '广汽本田', '廣汽本田', '东风本田', '東風本田', '一汽马自达', '一汽馬自達', '广汽三菱', '廣汽三菱')
# These suffixes are a search hint only and never produce an affiliation by themselves.
JAPANESE_LEGAL_SUFFIXES = ('株式会社', '合同会社', '有限会社', '日本株式会社')
PEER_BRANDS = ('日通', '近铁', '近鐵', '邮船', '郵船', '山九', '丸全昭和', 'yusen', 'kwe', 'nippon express')
PEER_ACTIVITY = ('物流', '运输', '運輸', '货运', '貨運', '供应链', '供應鏈', '通运', '通運', '储运', '儲運', '快递', '快遞', '仓储', '倉儲', '货代', '海运', '空运', '邮船', '郵船', 'logistics', 'freight', 'transport')
