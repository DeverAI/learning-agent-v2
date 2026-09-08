from fastapi import APIRouter, HTTPException
from services.user_profile import load_profile, save_profile, add_paper_result
from pydantic import BaseModel, Field, field_validator, model_validator

router = APIRouter(prefix="/api/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    role: str = "student"
    school_level: str = ""
    grade: str = ""
    province: str = ""
    city: str = ""
    school: str = ""
    teach_grade: str = ""
    favorite_subjects: list[str] = Field(default_factory=list, max_length=20)
    notes: str = ""
    style_notes: str = ""

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ("student", "teacher"):
            raise ValueError("role 必须是 student 或 teacher")
        return value

    @field_validator("school_level", "grade", "province", "city", "school", "teach_grade", "notes", "style_notes")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()[:2000]

    @field_validator("favorite_subjects")
    @classmethod
    def clean_subjects(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(str(item).strip()[:50] for item in value if str(item).strip()))


class PaperScore(BaseModel):
    paper_id: str
    title: str
    score: float = Field(ge=0, le=1000000)
    total: float = Field(100, gt=0, le=1000000)

    @model_validator(mode="after")
    def validate_score(self):
        if self.score > self.total:
            raise ValueError("得分不能超过总分")
        return self


@router.get("")
async def get_profile():
    return load_profile()


@router.put("")
async def update_profile(data: ProfileUpdate):
    p = load_profile()
    p["role"] = data.role
    p["school_level"] = data.school_level
    p["grade"] = data.grade
    p["province"] = data.province
    p["city"] = data.city
    p["school"] = data.school
    p["teach_grade"] = data.teach_grade
    p["favorite_subjects"] = data.favorite_subjects
    p["notes"] = data.notes
    p["style_notes"] = data.style_notes
    save_profile(p)
    return {"message": "已保存", "profile": p}


@router.post("/score")
async def record_score(data: PaperScore):
    from models.database import async_session
    from models.models import Paper
    async with async_session() as db:
        paper = await db.get(Paper, data.paper_id)
        if not paper:
            raise HTTPException(status_code=404, detail="试卷不存在")
        paper.user_score = data.score
        await db.commit()
        paper_title = paper.title
    add_paper_result(data.paper_id, paper_title, data.score, data.total)
    return {"message": "已记录", "avg_score": load_profile().get("avg_score")}
