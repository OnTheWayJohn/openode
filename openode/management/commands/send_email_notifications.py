import datetime
import logging

from django.core.management.base import NoArgsCommand
from django.core.urlresolvers import reverse
from django.db import connection
from django.db.models import Q, F
from openode.models import User, Post, PostRevision
from openode.models import Activity, EmailFeedSetting
from django.utils.translation import ugettext as _
from django.utils.translation import activate
from django.conf import settings as django_settings
from openode.conf import settings as openode_settings
from django.utils.datastructures import SortedDict
from django.contrib.contenttypes.models import ContentType
from openode import const
from openode import mail

DEBUG_THIS_COMMAND = getattr(django_settings, 'DEBUG_SEND_EMAIL_NOTIFICATIONS', True)


def get_all_origin_posts(mentions):
    origin_posts = set()
    for mention in mentions:
        post = mention.content_object
        origin_posts.add(post.get_origin_post())
    return list(origin_posts)


#todo: refactor this as class
def extend_question_list(
                    src, dst, cutoff_time=None,
                    limit=False, add_mention=False,
                    add_comment=False
                ):
    """src is a query set with questions
    or None
    dst - is an ordered dictionary
    update reporting cutoff time for each question
    to the latest value to be more permissive about updates
    """
    if src is None:  # is not QuerySet
        return  # will not do anything if subscription of this type is not used
    # if limit and len(dst.keys()) >= openode_settings.MAX_ALERTS_PER_EMAIL:
    #     return

    if cutoff_time is None:
        if hasattr(src, 'cutoff_time'):
            cutoff_time = src.cutoff_time
        else:
            raise ValueError('cutoff_time is a mandatory parameter')

    for q in src:
        if q in dst:
            meta_data = dst[q]
        else:
            meta_data = {'cutoff_time': cutoff_time}
            dst[q] = meta_data

        if cutoff_time > meta_data['cutoff_time']:
            #the latest cutoff time wins for a given question
            #if the question falls into several subscription groups
            #this makes mailer more eager in sending email
            meta_data['cutoff_time'] = cutoff_time
        if add_comment:
            if 'comments' in meta_data:
                meta_data['comments'] += 1
            else:
                meta_data['comments'] = 1


def format_action_count(string, number, output):
    if number > 0:
        output.append(_(string) % {'num': number})


class Command(NoArgsCommand):
    def handle_noargs(self, **options):
        if openode_settings.ENABLE_EMAIL_ALERTS:
            try:
                try:
                    self.send_email_alerts()
                except Exception, e:
                    print e
            finally:
                connection.close()

    def get_updated_threads_for_user(self, user):
        """
        retreive relevant question updates for the user
        according to their subscriptions and recorded question
        views
        """
        # set default language TODO - language per user - add user atribute
        activate(django_settings.LANGUAGE_CODE)

        user_feeds = EmailFeedSetting.objects.filter(
                                                subscriber=user
                                            ).exclude(
                                                frequency__in=('n', 'i')
                                            )

        should_proceed = False
        for feed in user_feeds:
            if feed.should_send_now() == True:
                should_proceed = True
                break

        #shortcircuit - if there is no ripe feed to work on for this user
        if should_proceed == False:
            logging.debug(u'Notification: %s not send - should proceed = False' % user.screen_name)
            return {}

        #these are placeholders for separate query sets per question group
        #there are four groups - one for each EmailFeedSetting.feed_type
        #and each group has subtypes A and B
        #that's because of the strange thing commented below
        #see note on Q and F objects marked with todo tag
        q_sel_A = None
        q_sel_B = None

        # q_ask_A = None
        # q_ask_B = None

        # q_ans_A = None
        # q_ans_B = None

        # q_all_A = None
        # q_all_B = None

        #base question query set for this user
        #basic things - not deleted, not closed, not too old
        #not last edited by the same user
        base_qs = Post.objects.filter(post_type__in=[const.POST_TYPE_DISCUSSION, const.POST_TYPE_QUESTION, const.POST_TYPE_DOCUMENT]).exclude(
                                thread__last_activity_by=user
                            ).exclude(
                                thread__last_activity_at__lt=user.date_joined  # exclude old stuff
                            ).exclude(
                                deleted=True
                            ).exclude(
                                thread__closed=True
                            ).order_by('-thread__last_activity_at')
        #todo: for some reason filter on did not work as expected ~Q(viewed__who=user) |
        #      Q(viewed__who=user,viewed__when__lt=F('thread__last_activity_at'))
        #returns way more questions than you might think it should
        #so because of that I've created separate query sets Q_set2 and Q_set3
        #plus two separate queries run faster!

        #build two two queries based
        #questions that are not seen by the user at all
        not_seen_qs = base_qs.filter(~Q(thread__viewed__user=user))
        #questions that were seen, but before last modification
        seen_before_last_mod_qs = base_qs.filter(
                                    Q(
                                        thread__viewed__user=user,
                                        thread__viewed__last_visit__lt=F('thread__last_activity_at')
                                    )
                                )

        #shorten variables for convenience
        Q_set_A = not_seen_qs
        Q_set_B = seen_before_last_mod_qs

        for feed in user_feeds:
            #each group of updates represented by the corresponding
            #query set has it's own cutoff time
            #that cutoff time is computed for each user individually
            #and stored as a parameter "cutoff_time"

            #we won't send email for a given question if an email has been
            #sent after that cutoff_time
            if feed.should_send_now():
                feed.mark_reported_now()
                cutoff_time = feed.get_previous_report_cutoff_time()

                if feed.feed_type == 'q_sel':
                    q_sel_A = Q_set_A.filter(Q(thread__followed_by=user) | Q(thread__node__followed_by=user))
                    q_sel_A.cutoff_time = cutoff_time  # store cutoff time per query set
                    q_sel_B = Q_set_B.filter(thread__followed_by=user)
                    q_sel_B.cutoff_time = cutoff_time  # store cutoff time per query set
                    # print q_sel_A, q_sel_B
                # elif feed.feed_type == 'q_ask':
                #     q_ask_A = Q_set_A.filter(author=user)
                #     q_ask_A.cutoff_time = cutoff_time
                #     q_ask_B = Q_set_B.filter(author=user)
                #     q_ask_B.cutoff_time = cutoff_time

                # elif feed.feed_type == 'q_ans':
                #     q_ans_A = Q_set_A.filter(thread__posts__author=user, thread__posts__post_type='answer')
                #     q_ans_A = q_ans_A[:openode_settings.MAX_ALERTS_PER_EMAIL]
                #     q_ans_A.cutoff_time = cutoff_time

                #     q_ans_B = Q_set_B.filter(thread__posts__author=user, thread__posts__post_type='answer')
                #     q_ans_B = q_ans_B[:openode_settings.MAX_ALERTS_PER_EMAIL]
                #     q_ans_B.cutoff_time = cutoff_time

                # elif feed.feed_type == 'q_all':
                #     q_all_A = user.get_tag_filtered_questions(Q_set_A)
                #     q_all_B = user.get_tag_filtered_questions(Q_set_B)

                #     q_all_A = q_all_A[:openode_settings.MAX_ALERTS_PER_EMAIL]
                #     q_all_B = q_all_B[:openode_settings.MAX_ALERTS_PER_EMAIL]
                #     q_all_A.cutoff_time = cutoff_time
                #     q_all_B.cutoff_time = cutoff_time

        #build ordered list questions for the email report
        q_list = SortedDict()

        #todo: refactor q_list into a separate class?
        extend_question_list(q_sel_A, q_list)
        extend_question_list(q_sel_B, q_list)

        #build list of comment responses here
        #it is separate because posts are not marked as changed

        # extend_question_list(q_ask_A, q_list, limit=True)
        # extend_question_list(q_ask_B, q_list, limit=True)

        # extend_question_list(q_ans_A, q_list, limit=True)
        # extend_question_list(q_ans_B, q_list, limit=True)

        # if user.email_tag_filter_strategy == const.EXCLUDE_IGNORED:
        #     extend_question_list(q_all_A, q_list, limit=True)
        #     extend_question_list(q_all_B, q_list, limit=True)

        ctype = ContentType.objects.get_for_model(Post)
        EMAIL_UPDATE_ACTIVITY = const.TYPE_ACTIVITY_EMAIL_UPDATE_SENT

        #up to this point we still don't know if emails about
        #collected questions were sent recently
        #the next loop examines activity record and decides
        #for each question, whether it needs to be included or not
        #into the report

        for q, meta_data in q_list.items():
            #this loop edits meta_data for each question
            #so that user will receive counts on new edits new answers, etc
            #and marks questions that need to be skipped
            #because an email about them was sent recently enough

            #also it keeps a record of latest email activity per question per user
            try:
                #todo: is it possible to use content_object here, instead of
                #content type and object_id pair?
                update_info = Activity.objects.get(
                                            user=user,
                                            content_type=ctype,
                                            object_id=q.id,
                                            activity_type=EMAIL_UPDATE_ACTIVITY
                                        )
                emailed_at = update_info.active_at
            except Activity.DoesNotExist:
                update_info = Activity(
                                        user=user,
                                        content_object=q,
                                        activity_type=EMAIL_UPDATE_ACTIVITY
                                    )
                emailed_at = datetime.datetime(1970, 1, 1)  # long time ago
            except Activity.MultipleObjectsReturned:
                raise Exception(
                                'server error - multiple question email activities '
                                'found per user-question pair'
                                )

            cutoff_time = meta_data['cutoff_time']  # cutoff time for the question

            #skip question if we need to wait longer because
            #the delay before the next email has not yet elapsed
            #or if last email was sent after the most recent modification
            if emailed_at > cutoff_time or emailed_at > q.thread.last_activity_at:
                meta_data['skip'] = True
                continue

            #collect info on all sorts of news that happened after
            #the most recent emailing to the user about this question
            q_rev = q.revisions.filter(revised_at__gt=emailed_at).exclude(author=user)

            #now update all sorts of metadata per question
            meta_data['q_rev'] = len(q_rev)
            if len(q_rev) > 0 and q.added_at == q_rev[0].revised_at:
                meta_data['q_rev'] = 0
                meta_data['new_q'] = True
            else:
                meta_data['new_q'] = False

            new_ans = Post.objects.get_answers(user).filter(
                thread=q.thread,
                added_at__gt=emailed_at,
                deleted=False,
            ).exclude(author=user)
            meta_data['new_ans'] = len(new_ans)

            ans_rev = PostRevision.objects.filter(
                post__post_type=const.POST_TYPE_THREAD_POST,
                post__thread=q.thread,
                revised_at__gt=emailed_at,
                post__deleted=False,
                revision__gte=2,
            ).exclude(author=user)
            meta_data['ans_rev'] = len(ans_rev)

            comments = meta_data.get('comments', 0)

            #print meta_data
            #finally skip question if there are no news indeed
            if len(q_rev) + len(new_ans) + len(ans_rev) + comments == 0:
                meta_data['skip'] = True
                #print 'skipping'
            else:
                meta_data['skip'] = False
                #print 'not skipping'
                update_info.active_at = datetime.datetime.now()
                if DEBUG_THIS_COMMAND == False:
                    update_info.save()  # save question email update activity
        #q_list is actually an ordered dictionary
        #print 'user %s gets %d' % (user.username, len(q_list.keys()))
        #todo: sort question list by update time
        return q_list

    def send_email_alerts(self):
        #does not change the database, only sends the email
        #todo: move this to template
        for user in User.objects.filter(is_active=True):
            user.add_missing_openode_subscriptions()
            #todo: q_list is a dictionary, not a list

            # print 'user:', user
            q_list = self.get_updated_threads_for_user(user)
            # print q_list
            if len(q_list.keys()) == 0:
                continue
            num_q = 0
            for question, meta_data in q_list.items():
                if meta_data['skip']:
                    del q_list[question]
                else:
                    num_q += 1

            logging.info(u'Notification: %s (pk=%s) will recieve %s notices' % (user.screen_name, user.pk, num_q))
            # print 'pocet:', num_q
            if num_q > 0:
                url_prefix = openode_settings.APP_URL

                # threads = Thread.objects.filter(id__in=[qq.thread_id for qq in q_list.keys()])
                # tag_summary = Thread.objects.get_tag_summary_from_threads(threads) #TODO
                # tag_summary = ''
                # question_count = len(q_list.keys())

                subject_line = _('Updates overview')

                #todo: send this to special log
                #print 'have %d updated questions for %s' % (num_q, user.username)
                text = ''

                items_added = 0
                # items_unreported = 0
                for q, meta_data in q_list.items():
                    act_list = []
                    if meta_data['skip']:
                        continue
                    # if items_added >= openode_settings.MAX_ALERTS_PER_EMAIL:
                    #     items_unreported = num_q - items_added  # may be inaccurate actually, but it's ok

                    else:
                        items_added += 1
                        if meta_data['new_q']:
                            if q.post_type == const.POST_TYPE_QUESTION:
                                act_list.append(_('new question'))
                            elif q.post_type == const.POST_TYPE_DISCUSSION:
                                act_list.append(_('new discussion'))
                            elif q.post_type == const.POST_TYPE_DOCUMENT:
                                act_list.append(_('new document'))

                        if meta_data['q_rev']:
                            if q.post_type == const.POST_TYPE_QUESTION:
                                act_list.append(_('changed question'))
                            elif q.post_type == const.POST_TYPE_DISCUSSION:
                                act_list.append(_('changed discussion'))
                            elif q.post_type == const.POST_TYPE_DOCUMENT:
                                act_list.append(_('changed document'))

                        if meta_data['new_ans']:
                            if q.post_type == const.POST_TYPE_QUESTION:
                                act_list.append(_('%(num)d new answers') % {'num': meta_data['new_ans']})
                            elif q.post_type == const.POST_TYPE_DISCUSSION:
                                act_list.append(_('%(num)d new posts') % {'num': meta_data['new_ans']})

                        if meta_data['ans_rev']:
                            if q.post_type == const.POST_TYPE_QUESTION:
                                act_list.append(_('%(num)d changed answers') % {'num': meta_data['ans_rev']})
                            elif q.post_type == const.POST_TYPE_DISCUSSION:
                                act_list.append(_('%(num)d changed posts') % {'num': meta_data['ans_rev']})

                        # format_action_count('%(num)d rev', meta_data['q_rev'], act_list)
                        # format_action_count('%(num)d ans', meta_data['new_ans'], act_list)
                        # format_action_count('%(num)d ans rev', meta_data['ans_rev'], act_list)
                        act_token = ', '.join(act_list)
                        text += '<a href="%s">%s</a> (%s)<br />' % (url_prefix + q.get_absolute_url(), q.thread.title, act_token)

                #if len(q_list.keys()) >= openode_settings.MAX_ALERTS_PER_EMAIL:
                #    text += _('There may be more questions updated since '
                #                'you have logged in last time as this list is '
                #                'abridged for your convinience. Please visit '
                #                'the openode and see what\'s new!<br>'
                #              )

                # link = url_prefix + reverse(
                #                         'user_profile',
                #                         kwargs={
                #                             'id': user.id,
                #                             'tab_name': 'email_subscriptions'
                #                         }
                #                     )

                text += '<hr />'
                text += '<p>'
                text += _('Please remember that you can always adjust frequency of the email updates or turn them off entirely in your profile.')
                text += '</p>'
                text += '<p>'
                text += _('If you believe that this message was sent in an error, please contact us.')
                text += '</p>'

                if DEBUG_THIS_COMMAND == True:
                    recipient_email = django_settings.ADMINS[0][1]
                else:
                    recipient_email = user.email
                # print 'odeslan email'

                data = {
                    'text': text,
                    'site_name': openode_settings.APP_SHORT_NAME,
                    'site_url': openode_settings.APP_URL
                }
                from openode.skins.loaders import get_template
                template = get_template('email/notification.html')
                message = template.render(data)
                mail.send_mail(subject_line, message, django_settings.DEFAULT_FROM_EMAIL, [recipient_email], raise_on_failure=True)
                logging.info('Notification: %s (pk=%s) mail sent' % (user.screen_name, user.pk))

                # mail.send_mail(
                #     subject_line=subject_line,
                #     body_text=text,
                #     recipient_list=[recipient_email]
                # )