# مرصد الأسواق

لوحة عربية للسوق السعودي: مؤشرات وأسعار حيّة، خريطة حرارية للأسهم، خريطة أثر الأخبار على القطاعات، والمستفيد الأكبر من كل خبر.

## كيف يعمل

```
GitHub Actions (كل 15 دقيقة أثناء الجلسة، وكل ساعتين خارجها)
  ├─ scripts/fetch_market.py   → data/market.json   (المؤشرات والأسهم مع المصدر ووقت البيانات بالثانية)
  ├─ scripts/fetch_news.py     → data/news_raw.json (أخبار RSS من مصادر سعودية وعالمية)
  └─ scripts/analyze_news.py   → data/news.json     (التصنيف، المستفيد/المتضرر، أثر القطاعات)
  ثم يُرفع الموقع إلى GitHub Pages
site/index.html يقرأ data/*.json ويعرضها. إذا تعذر التحميل يعرض نسخة ثابتة مع تنبيه.
```

## التشغيل محليًا

```bash
pip install -r scripts/requirements.txt
python scripts/fetch_market.py --out data/market.json
python scripts/fetch_news.py --out data/news_raw.json
python scripts/analyze_news.py --in data/news_raw.json --out data/news.json --market data/market.json
python -m http.server 8765   # ثم افتح http://127.0.0.1:8765/site/
python -m pytest -q tests
```

## الإعدادات الاختيارية

- `ANTHROPIC_API_KEY` كسرّ في المستودع: يفعّل التحليل العربي للأخبار بنموذج Claude. بدونه يعمل التحليل بالقواعد.
- GitHub Pages يُفعَّل تلقائيًا من الـ workflow عند أول تشغيل (المصدر: GitHub Actions).

## تنبيه

أداة رصد وتحليل، وليست توصية استثمارية.
