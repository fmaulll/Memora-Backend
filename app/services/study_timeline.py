from datetime import date, timedelta

from app.schemas.ai import StudyDay, StudyTimeline


class StudyTimelineService:

    def generate(
        self,
        total_cards: int,
        target_date: date | None,
        study_purpose: str,
    ) -> StudyTimeline | None:

        # No deadline = no fixed timeline yet
        if target_date is None:
            return None

        today = date.today()

        # Include today as the first study day
        total_days = (target_date - today).days + 1

        if total_days <= 0:
            raise ValueError(
                "Target date must be in the future"
            )

        daily_plan: list[StudyDay] = []

        # Reserve the final day for review when preparing
        # for an exam or certification.
        is_exam_preparation = study_purpose in [
            "Prepare for an Exam",
            "Prepare for a Certification",
        ]

        learning_days = total_days

        if is_exam_preparation and total_days > 1:
            learning_days -= 1

        # Calculate base number of cards per learning day
        base_cards = total_cards // learning_days
        remainder = total_cards % learning_days

        remaining_cards = total_cards

        for day_index in range(learning_days):

            cards_today = base_cards

            # Distribute remaining cards across early days
            if day_index < remainder:
                cards_today += 1

            current_date = today + timedelta(
                days=day_index
            )

            focus = self._learning_focus(
                day_index=day_index,
                total_days=learning_days,
            )

            daily_plan.append(
                StudyDay(
                    day=day_index + 1,
                    date=current_date,
                    new_cards=cards_today,
                    focus=focus,
                )
            )

            remaining_cards -= cards_today

        # Final review day
        if is_exam_preparation and total_days > 1:

            daily_plan.append(
                StudyDay(
                    day=total_days,
                    date=target_date,
                    new_cards=0,
                    focus=(
                        "Review previously learned cards "
                        "and focus on difficult concepts"
                    ),
                )
            )

        return StudyTimeline(
            total_days=total_days,
            total_cards=total_cards,
            daily_plan=daily_plan,
        )

    def _learning_focus(
        self,
        day_index: int,
        total_days: int,
    ) -> str:

        if day_index == 0:
            return (
                "Start with foundational concepts and "
                "build your understanding"
            )

        if day_index == total_days - 1:
            return (
                "Complete the remaining concepts and "
                "review difficult material"
            )

        return (
            "Learn new cards and review previously "
            "studied concepts"
        )