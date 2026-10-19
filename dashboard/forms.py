from django import forms

class LookupForm(forms.Form):
    query = forms.CharField(label='Search Query', max_length=100, required=False)
    selected_member = forms.ChoiceField(label='Select a Member', choices=[], required=False)

    def __init__(self, *args, **kwargs):
        results = kwargs.pop('results', None)
        super(LookupForm, self).__init__(*args, **kwargs)
        if results:
            choices = [(str(result['id']), result['name']) for result in results]
            self.fields['selected_member'].choices = choices
