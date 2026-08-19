from django.db import transaction
from django.utils import timezone

from .models import Substitution, SubstitutionOffer


def expire_stale_offers() -> None:
    """Flip PENDING offers whose window has already passed to EXPIRED. Called
    at the top of the views that read offers - no cron/Celery needed."""
    SubstitutionOffer.objects.filter(
        status=SubstitutionOffer.Status.PENDING, end_datetime__lt=timezone.now()
    ).update(status=SubstitutionOffer.Status.EXPIRED, responded_at=timezone.now())


def create_offer(absence, candidate, start_dt, end_dt) -> tuple[SubstitutionOffer, bool]:
    """Get-or-create a PENDING offer for this exact candidate+slot, so a
    double submit reuses the existing offer instead of duplicating it and
    re-emailing the candidate. Returns (offer, created)."""
    return SubstitutionOffer.objects.get_or_create(
        absence=absence,
        substitute_teacher=candidate,
        start_datetime=start_dt,
        end_datetime=end_dt,
        status=SubstitutionOffer.Status.PENDING,
    )


def accept_offer(offer: SubstitutionOffer) -> Substitution | None:
    """Confirm `offer`: create the Substitution and expire any sibling
    pending offers for the same absence that now overlap it. Returns the new
    Substitution, or None if the slot or candidate is no longer available (the
    offer is left EXPIRED in that case) or the offer had already been
    responded to."""
    with transaction.atomic():
        offer = SubstitutionOffer.objects.select_for_update().get(pk=offer.pk)
        if offer.status != SubstitutionOffer.Status.PENDING:
            return None

        overlapping = Substitution.objects.filter(
            start_datetime__lt=offer.end_datetime, end_datetime__gt=offer.start_datetime
        )
        slot_taken = overlapping.filter(absence=offer.absence).exists()
        candidate_busy = overlapping.filter(substitute_teacher=offer.substitute_teacher).exists()
        if slot_taken or candidate_busy:
            offer.status = SubstitutionOffer.Status.EXPIRED
            offer.responded_at = timezone.now()
            offer.save(update_fields=["status", "responded_at"])
            return None

        substitution = Substitution.objects.create(
            absence=offer.absence,
            substitute_teacher=offer.substitute_teacher,
            start_datetime=offer.start_datetime,
            end_datetime=offer.end_datetime,
        )
        offer.status = SubstitutionOffer.Status.ACCEPTED
        offer.responded_at = timezone.now()
        offer.resulting_substitution = substitution
        offer.save(update_fields=["status", "responded_at", "resulting_substitution"])

        SubstitutionOffer.objects.filter(
            absence=offer.absence,
            status=SubstitutionOffer.Status.PENDING,
            start_datetime__lt=offer.end_datetime,
            end_datetime__gt=offer.start_datetime,
        ).exclude(pk=offer.pk).update(status=SubstitutionOffer.Status.EXPIRED, responded_at=timezone.now())

    return substitution


def decline_offer(offer: SubstitutionOffer) -> bool:
    """Mark `offer` declined. Returns whether it was actually pending (and so
    really got declined) - False if it had already been responded to."""
    updated = SubstitutionOffer.objects.filter(pk=offer.pk, status=SubstitutionOffer.Status.PENDING).update(
        status=SubstitutionOffer.Status.DECLINED, responded_at=timezone.now()
    )
    return bool(updated)
